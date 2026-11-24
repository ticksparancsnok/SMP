# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Remote control sessions: a teacher sees one PC's screen live and uses its mouse and
keyboard (memory only, one server process, like watch.py).

    teacher's browser  <-- WebSocket -->  server  <-- signed HTTP -->  PC's agent
       input events  ------------------->  queue  -----(long poll)-->  SendInput
       JPEG frames   <-------------------  latest <-----(PUT)--------  screen capture

A session lives exactly as long as the teacher's WebSocket: closing the view, losing
the connection or a server restart ends it. The PC learns about it from its config
("remote" in the classroom section) and stops when that is gone.

Privacy: frames and input pass through memory and are dropped; keystrokes are never
stored or logged. Starting and ending a session (who, which PC, how long) is audited.

Student input: shared by default (student and teacher both use mouse and keyboard). The
teacher can block the student's own keyboard and mouse for the session (`block_local`);
the PC then lets only the teacher's input through. Ctrl+Alt+Del still works (Windows
doesn't let any program block it); back from its screen, the block holds again.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

INPUT_POLL_S = 20.0  # an agent's input request waits at most this long (its timeout is 30 s)
MAX_PENDING_EVENTS = 1000  # input waiting for the agent; the oldest go first if it falls behind
MAX_EVENTS_PER_MESSAGE = 200
_KEY_CODE = re.compile(r"^[A-Za-z0-9]{1,24}$")  # KeyboardEvent.code: "KeyA", "ShiftLeft", "Numpad7", ...


@dataclass
class RemoteFrame:
    jpeg: bytes
    width: int
    height: int
    screen_width: int
    screen_height: int
    at: float = field(default_factory=time.time)


class RemoteSession:
    """One teacher controlling one PC. Waiting is done with asyncio.Event: the
    WebSocket waits for frames, the agent's input request waits for input."""

    def __init__(self, device_id: uuid.UUID, room_id: uuid.UUID | None, org_id: uuid.UUID, hostname: str,
                 user_id: uuid.UUID, user_name: str) -> None:
        self.id = secrets.token_hex(12)  # in the PC's config; it sends it back with every request
        self.device_id, self.room_id, self.org_id, self.hostname = device_id, room_id, org_id, hostname
        self.user_id, self.user_name = user_id, user_name
        self.started = time.monotonic()
        self.frame: RemoteFrame | None = None
        self.frame_ready = asyncio.Event()  # a new frame (or the end) for the WebSocket
        self._events: list[dict[str, Any]] = []
        self._input_ready = asyncio.Event()  # input (or the end) for the agent
        self.ended = asyncio.Event()
        self.end_reason = ""
        self.block_local = False  # the student's own keyboard and mouse blocked (teacher's choice)

    def seconds(self) -> int:
        return round(time.monotonic() - self.started)

    def put_frame(self, frame: RemoteFrame) -> None:
        self.frame = frame
        self.frame_ready.set()

    def push_input(self, events: list[dict[str, Any]]) -> None:
        self._events.extend(events)
        if len(self._events) > MAX_PENDING_EVENTS:
            del self._events[: len(self._events) - MAX_PENDING_EVENTS]
        self._input_ready.set()

    async def take_input(self, wait_s: float = INPUT_POLL_S) -> list[dict[str, Any]] | None:
        """The input waiting for the agent ([] after `wait_s` without any); None once
        the session has ended."""
        if not self._events and not self.ended.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait_s):
                    await self._input_ready.wait()
        if self.ended.is_set():
            return None
        events, self._events = self._events, []
        self._input_ready.clear()
        return coalesce_moves(events)

    def end(self, reason: str) -> None:
        """Wake everything waiting on this session; it is over."""
        if not self.ended.is_set():
            self.end_reason = reason
            self.ended.set()
            self._input_ready.set()
            self.frame_ready.set()


class RemoteSessions:
    """The active sessions, at most one per PC."""

    def __init__(self) -> None:
        self._by_device: dict[uuid.UUID, RemoteSession] = {}

    def get(self, device_id: uuid.UUID) -> RemoteSession | None:
        return self._by_device.get(device_id)

    def start(self, session: RemoteSession) -> RemoteSession | None:
        """Make `session` the PC's session; returns the one it replaced (ended)."""
        old = self._by_device.get(session.device_id)
        if old is not None:
            old.end("taken over")
        self._by_device[session.device_id] = session
        return old

    def finish(self, session: RemoteSession, reason: str) -> None:
        session.end(reason)
        if self._by_device.get(session.device_id) is session:
            del self._by_device[session.device_id]

    def end_devices(self, device_ids: set[uuid.UUID], reason: str) -> None:
        for device_id in device_ids:
            if (s := self._by_device.get(device_id)) is not None:
                s.end(reason)  # its WebSocket notices and finishes it

    def controllers(self) -> dict[uuid.UUID, str]:
        """Who controls which PC (for the Monitoring page)."""
        return {d: s.user_name for d, s in self._by_device.items() if not s.ended.is_set()}


def coalesce_moves(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Of several mouse moves in a row only the last matters: fewer events to replay."""
    out: list[dict[str, Any]] = []
    for e in events:
        if e["t"] == "move" and out and out[-1]["t"] == "move":
            out[-1] = e
        else:
            out.append(e)
    return out


def clean_events(raw: Any) -> list[dict[str, Any]]:
    """The browser's input, checked: only the shapes the agent understands, numbers in
    range; anything else is dropped. Positions are 0..1 across the PC's primary screen."""
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        return []
    out: list[dict[str, Any]] = []
    for e in raw["events"][:MAX_EVENTS_PER_MESSAGE]:
        if not isinstance(e, dict):
            continue
        kind, down = e.get("t"), e.get("down")
        if kind == "move" and _is_number(e.get("x")) and _is_number(e.get("y")):
            out.append({"t": "move", "x": min(max(float(e["x"]), 0.0), 1.0), "y": min(max(float(e["y"]), 0.0), 1.0)})
        elif kind == "button" and e.get("b") in (0, 1, 2) and isinstance(down, bool):
            out.append({"t": "button", "b": e["b"], "down": down})
        elif kind == "wheel" and _is_number(e.get("dy", 0)) and _is_number(e.get("dx", 0)):
            dy, dx = (max(-2400, min(2400, int(e.get(k, 0)))) for k in ("dy", "dx"))
            if dy or dx:
                out.append({"t": "wheel", "dy": dy, "dx": dx})
        elif kind == "key" and isinstance(e.get("code"), str) and _KEY_CODE.match(e["code"]) and isinstance(down, bool):
            out.append({"t": "key", "code": e["code"], "down": down})
    return out


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and v == v  # not NaN
