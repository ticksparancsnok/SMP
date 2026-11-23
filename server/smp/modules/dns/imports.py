# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Hosts-file imports as background jobs, so a big list (StevenBlack's: 76k domains)
never stalls the rest of SMP, and the UI can show how far it got.

    POST   /dns/blocklist/imports               the file (raw body) -> a job, checking
    GET    /dns/blocklist/imports/{id}          state, progress (0-1), counts
    GET    /dns/blocklist/imports/{id}/domains  ?kind=new|existing|skipped, paged: the preview
    POST   /dns/blocklist/imports/{id}/confirm  add the new domains -> saving, syncing, done
    DELETE /dns/blocklist/imports/{id}          forget it (not while saving)

States: checking -> ready -> (confirm) saving -> syncing -> done; or failed.

  checking  parse + sort out in a worker thread: new / already on the blocklist /
            skipped (allowlisted, public suffix). The file is uploaded once and kept
            only until then.
  saving    one transaction: the domains are COPYed (Postgres' bulk load) into a
            temporary table in chunks of CHUNK, then one INSERT ... SELECT ... ON
            CONFLICT DO NOTHING (a domain someone added meanwhile is fine). Between
            chunks the server answers other requests; the audit row goes in the same
            transaction.
  syncing   the sinkhole gets the full lists (api.sync_org: off the event loop too).

Jobs live in this server process's memory (like monitoring state: SMP runs one
process), belong to the user who started them, and are forgotten 30 minutes after
they were last looked at.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from smp.core import db
from smp.core.events import Event
from smp.core.models import AuditLog
from smp.modules.dns.domains import by_domain, is_public_suffix, match, parse_hosts
from smp.modules.dns.models import DnsAllowlistEntry, DnsBlocklistEntry

MAX_IMPORT_BYTES = 50 * 1024 * 1024  # nginx allows 64 MB; the biggest common lists are ~10 MB
CHUNK = 10_000
KEEP_FOR_S = 30 * 60
MAX_JOBS_PER_ORG = 5

States = ("checking", "ready", "saving", "syncing", "done", "failed")


@dataclass
class Actor:
    """Who confirmed the import, for the audit row written by the background task."""

    kind: str
    id: str
    name: str
    ip: str | None
    user_agent: str | None


@dataclass
class ImportJob:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    owner_name: str
    file_bytes: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "checking"
    progress: float = 0.0  # of the current state, 0..1
    new: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    added: int | None = None  # after saving: rows actually inserted
    sync_ok: bool | None = None
    error: dict[str, Any] | None = None  # {"code", "detail", "params"}
    touched: float = field(default_factory=time.monotonic)

    @property
    def busy(self) -> bool:
        return self.state in ("checking", "saving", "syncing")

    def fail(self, code: str, detail: str, **params: Any) -> None:
        self.state, self.error = "failed", {"code": code, "detail": detail, "params": params}


class ImportJobs:
    def __init__(self) -> None:
        self._jobs: dict[str, ImportJob] = {}
        self._tasks: set[asyncio.Task] = set()  # keep running tasks referenced

    def _forget_old(self) -> None:
        now = time.monotonic()
        for job_id, job in list(self._jobs.items()):
            if not job.busy and now - job.touched > KEEP_FOR_S:
                del self._jobs[job_id]

    def add(self, job: ImportJob) -> bool:
        """False if the school already has MAX_JOBS_PER_ORG imports going."""
        self._forget_old()
        if sum(1 for j in self._jobs.values() if j.org_id == job.org_id) >= MAX_JOBS_PER_ORG:
            return False
        self._jobs[job.id] = job
        return True

    def get(self, job_id: str, org_id: uuid.UUID, owner_id: uuid.UUID) -> ImportJob | None:
        job = self._jobs.get(job_id)
        if job is None or job.org_id != org_id or job.owner_id != owner_id:
            return None
        job.touched = time.monotonic()
        return job

    def remove(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def run(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


# --------------------------------------------------------------------------- checking


async def check(job: ImportJob, content: bytes) -> None:
    """Parse and sort out the file (worker thread), against the lists as they are now."""
    try:
        async with db.sessionmaker()() as s:
            allow_rows = (await s.execute(
                select(DnsAllowlistEntry.domain, DnsAllowlistEntry.include_subdomains)
                .where(DnsAllowlistEntry.org_id == job.org_id)
            )).all()
            on_list = set((await s.execute(
                select(DnsBlocklistEntry.domain).where(DnsBlocklistEntry.org_id == job.org_id)
            )).scalars())

        def work() -> None:
            def parsing(done: int, total: int) -> None:
                job.progress = 0.8 * done / max(total, 1)

            domains = parse_hosts(content, progress=parsing)
            allow = by_domain(allow_rows)  # rows have .domain and .include_subdomains
            for i, domain in enumerate(domains):
                if is_public_suffix(domain) or match(domain, allow) is not None:
                    job.skipped.append(domain)
                elif domain in on_list:
                    job.existing.append(domain)
                else:
                    job.new.append(domain)
                if i % 5000 == 0:
                    job.progress = 0.8 + 0.2 * i / max(len(domains), 1)

        await run_in_threadpool(work)
        job.state, job.progress = "ready", 1.0
    except Exception as e:  # noqa: BLE001 - reported to the user, logged
        import logging

        logging.getLogger("smp.dns").exception("checking a hosts import failed")
        job.fail("dns.import_failed", f"the file couldn't be read ({type(e).__name__})")


# --------------------------------------------------------------------------- saving


async def save(job: ImportJob, actor: Actor, bus, sync_org) -> None:
    """Insert the new domains in chunks (one transaction), audit, then sync the sinkhole."""
    added_by = f"imported by: {job.owner_name}"
    now = datetime.now(UTC)
    try:
        async with db.sessionmaker()() as s:
            await s.execute(select(1))  # opens the transaction the raw statements below run in
            pg = (await (await s.connection()).get_raw_connection()).driver_connection  # asyncpg
            assert pg.is_in_transaction()
            await pg.execute("CREATE TEMP TABLE dns_import (domain varchar(253)) ON COMMIT DROP")
            for start in range(0, len(job.new), CHUNK):
                chunk = job.new[start:start + CHUNK]
                await pg.copy_records_to_table("dns_import", records=[(d,) for d in chunk], columns=["domain"])
                job.progress = 0.9 * (start + len(chunk)) / len(job.new)
            result = await pg.execute(
                f"""INSERT INTO {DnsBlocklistEntry.__tablename__}
                      (id, org_id, domain, include_subdomains, reason, added_by, confidence, added_at, created_at, updated_at)
                    SELECT gen_random_uuid(), $1, domain, true, 'imported', $2, NULL, $3, now(), now() FROM dns_import
                    ON CONFLICT (org_id, domain) DO NOTHING""",
                job.org_id, added_by, now,
            )
            job.added = int(result.split()[-1])  # "INSERT 0 <rows>"
            job.progress = 1.0
            s.add(AuditLog(
                org_id=job.org_id, actor_type=actor.kind, actor_id=actor.id, actor_name=actor.name,
                action="dns.blocklist.import", target_type="dns_blocklist", target_id=None,
                details={"imported": job.added, "already_listed": len(job.existing), "skipped": len(job.skipped),
                         "addedBy": added_by},
                ip=actor.ip, user_agent=actor.user_agent,
            ))
            await s.commit()
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("smp.dns").exception("saving a hosts import failed")
        job.fail("dns.import_failed", "saving the domains failed; nothing was imported")
        return
    await bus.publish(Event(subject="dns.blocklist.updated", org_id=job.org_id, payload={"imported": job.added}))

    job.state, job.progress = "syncing", 0.0
    try:
        await sync_org(job.org_id)
        job.sync_ok = True
    except Exception:  # noqa: BLE001 - saved; the sinkhole gets it with the next sync
        job.sync_ok = False
    job.state, job.progress = "done", 1.0
    job.new, job.existing = [], []  # not needed any more (the counts stay)
