# Writing a module

`server/smp/modules/shortlinks/` is the reference. Copy its layout.

## 1. Create the package

```
server/smp/modules/dns/
  __init__.py   # the Module subclass (manifest)
  models.py     # tables, named dns_<something>
  api.py        # routers
```

```python
# __init__.py
from fastapi import APIRouter
from pydantic import BaseModel, Field
from smp.core.module import EventSubject, Module, Permission, UiRoute, Widget, WidgetItem


class DnsSettings(BaseModel):            # per-school settings, edited in the portal
    upstreams: list[str] = ["1.1.1.1", "9.9.9.9"]
    auto_block_threshold: float = Field(0.9, ge=0, le=1)


class DnsPolicy(BaseModel):              # validates Policy.data where kind == "dns"
    blocked_categories: list[str] = ["adult", "games"]
    extra_blocked: list[str] = []
    extra_allowed: list[str] = []


class DnsModule(Module):
    name = "dns"
    title = "Oldal blokkoló"
    permissions = (
        Permission("dns.review.manage", "Approve/reject classifier verdicts", ("teacher",)),
        Permission("dns.lists.write", "Edit block/allow lists"),          # admin only
        # Things that happen *in a room* are room_scoped: a teacher assigned to
        # Lab 1 gets this permission only for Lab 1.
        # Permission("classroom.screen.lock", "Lock screens", ("teacher",), room_scoped=True),
    )
    ui_routes = (UiRoute("/dns", "Oldal blokkoló", "🌐", "dns.review.manage"),)
    events = (EventSubject("dns.domain.blocked", "A domain was auto-blocked"),)
    settings_model = DnsSettings
    policy_model = DnsPolicy

    def router(self) -> APIRouter:
        from smp.modules.dns.api import router
        return router

    def subscribe(self, bus):
        bus.subscribe("core.policy.changed", self._on_policy_changed)

    async def _on_policy_changed(self, event): ...

    async def dashboard(self, ctx) -> list[Widget]:   # cards on the home page
        if not ctx.principal.can("dns.review.manage"):
            return []
        top = ...  # query your tables
        return [Widget(id="dns.top_blocked", kind="list", title="Top blokkolt domainek", icon="🌐",
                       link="/dns", order=100,
                       items=[WidgetItem(label=d, value=f"{n} lekérés") for d, n in top])]


from smp.modules.dns import models  # noqa: E402,F401  (so Alembic sees the tables)
```

## 2. Register it

Add `DnsModule()` to `all_modules()` in `server/smp/modules/__init__.py`.

## 3. Tables

```python
from smp.core.db import Base, OrgScoped, Timestamps, UUIDPk

class Verdict(UUIDPk, Timestamps, OrgScoped, Base):
    __tablename__ = "dns_verdicts"
    ...
```

- Always use the `OrgScoped` mixin, and prefix table names with the module name.
- Reference core entities by FK (`ForeignKey("devices.id")`, `ForeignKey("rooms.id")`).
- Then generate a migration and **read it** before committing:
  ```
  alembic revision --autogenerate -m "dns: verdicts table"
  ```

## 4. Handlers

```python
from fastapi import APIRouter, Depends
from smp.core.context import Ctx, require

router = APIRouter()   # mounted at /api/v1/dns

@router.post("/verdicts/{vid}/approve")
async def approve(vid: uuid.UUID, ctx: Ctx = Depends(require("dns.review.manage"))):
    v = await ctx.get(Verdict, vid)           # org-scoped, 404 otherwise
    v.status = "blocked"
    ctx.audit("dns.verdict.approve", v, {"domain": v.domain})
    ctx.emit("dns.domain.blocked", {"domain": v.domain})
    await ctx.commit()                         # commit, then publish events
    return v
```

Rules:
- **Query through `ctx.get()` / `ctx.select(Model)`.** They add the `org_id` filter. If you write raw `select()`, add `Model.org_id == ctx.org_id` yourself.
- **Audit every state change** that a human would ask about later ("who unblocked this?").
- **Commit explicitly** with `await ctx.commit()`. Nothing is committed automatically.
- **Room-scoped actions** (lock screens, exam mode): use `require(perm, any_room=True)` and then `ctx.check(perm, device.room_id)`.
- Read settings with `await ctx.settings("dns")`. It returns your pydantic model with defaults filled in.
- Effective policy for a device: `await effective_policies(ctx.db, ctx.org_id, device=d, kinds={"dns"})` (from `smp.core.policy`).

## 5. Services outside the Python process (DNS server, agent)

They authenticate with an API token that has only the permissions they need (e.g. `core.devices.read`, `core.policies.read`), and they should cache what they fetch so they keep working when the control plane is down. Put the endpoints they call (e.g. "full DNS policy snapshot") in your module's router.

## 6. Web UI page

The menu is built from your manifest's `ui_routes`, so the entry appears automatically (greyed-out placeholders for `dns`/`classroom`/`software` vanish once the module is enabled). To give it a page:

1. Regenerate the API types: `(cd server && smp openapi -o ../WEBUI/openapi.json) && (cd WEBUI && npm run gen:api)`
2. Add `WEBUI/src/views/DnsView.vue`. `RoomsView.vue` is a compact example of list + dialog + error handling.
3. Register the route in `WEBUI/src/router.ts` with `meta: { perm: '...' }`.

Use `api.get/post/...` from `src/api/client.ts`, and `can()`/`canAnywhere()` from `src/session.ts` to hide buttons the user can't use. The server still enforces everything.

## 7. Tests

Add `server/tests/test_<module>.py`. The fixtures `admin`, `anon`, `login_as(...)` and `org` (fresh DB per test) are in `tests/conftest.py`.
