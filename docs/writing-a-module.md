# Writing a module

`server/smp/modules/shortlinks/` is the reference. Copy its layout.

The examples below build an imagined, simplified `dns` module. The real one (`server/smp/modules/dns/`) came later and looks different (other permissions, no classifier yet), so use your own module's name instead.

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
    title = "Site blocker"        # all server text is English; the UI translates (section 6)
    permissions = (
        Permission("dns.review.manage", "Approve/reject classifier verdicts", ("teacher",)),
        Permission("dns.lists.write", "Edit block/allow lists"),          # admin only
        # Things that happen *in a room* are room_scoped: a teacher assigned to
        # Lab 1 gets this permission only for Lab 1.
        # Permission("classroom.screens.lock", "Lock screens", ("teacher",), room_scoped=True),
    )
    ui_routes = (UiRoute("/dns", "Site blocker", "🌐", "dns.review.manage"),)
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
        return [Widget(id="dns.top_blocked", kind="list", title="Top blocked domains", icon="🌐",
                       link="/dns", order=100,
                       items=[WidgetItem(label=d, value=f"{n} requests") for d, n in top])]
        # A "stat" card with a hint also sends the numbers behind it, so the UI can
        # rebuild the hint in the user's language:
        #   Widget(id="dns.blocked_today", title="Blocked today", value=n,
        #          hint=f"{clients} clients", data={"clients": clients}, ...)


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
- **Errors a user can see: raise `AppError`** (`smp.core.errors`) instead of `HTTPException`, with an English message, a stable code, and the values in the message as params:

  ```python
  raise AppError(409, "dns.domain_exists", "domain already exists")
  raise AppError(409, "policies.schedule_in_use", f"schedule is used by {n} assignment(s)", count=n)
  ```

  The response is `{"detail": "<English>", "code": "...", "params": {...}}`. The UI shows the translation of the code, or `detail` if it has none. Validators that collect several problems (like the .xlsx import) return `Problem(code, message, params)` objects the same way.

## 5. Services outside the Python process (DNS server, agent)

They authenticate with an API token that has only the permissions they need (e.g. `core.devices.read`, `core.policies.read`), and they should cache what they fetch so they keep working when the control plane is down. Put the endpoints they call (e.g. "full DNS policy snapshot") in your module's router.

**The agent on the PCs** works differently: no tokens, every request is signed with the PC's own key. To give PCs something to do:

1. Return your section from the module hook (keep it small; it is sent on every config change):

   ```python
   async def agent_config(self, db, device) -> dict | None:
       return {"active": True, "interval_s": 5}   # arrives at the agent as config["<module name>"]
   ```

   When something that changes the answer happens, publish `agent.config.changed` (or one of the `core.*` events the agent module already listens to): waiting agents get the new config at once.
2. Endpoints the agent calls use `smp.modules.agent.api.authenticate_agent(request)` (returns the device and the raw body; rejects unsigned, unknown or replayed requests).
3. In `agent/`, add a capability that reads your section (see `agent/README.md`, "Adding a capability"). The screen monitor (`classroom` module + `capabilities/screen`) is the worked example.

## 6. Web UI page

The menu is built from your manifest's `ui_routes`, so the entry appears automatically. To give it a page:

1. Regenerate the API types: `(cd server && smp openapi -o ../WEBUI/openapi.json) && (cd WEBUI && npm run gen:api)`
2. Add `WEBUI/src/views/DnsView.vue`. `RoomsView.vue` is a compact example of list + dialog + error handling.
3. Register the route in `WEBUI/src/router.ts` with `meta: { perm: '...' }`.

Use `api.get/post/...` from `src/api/client.ts`, and `can()`/`canAnywhere()` from `src/session.ts` to hide buttons the user can't use. The server still enforces everything.

**Languages (HU/EN).** The server speaks English only; the web UI translates. Write every UI string as `t('dns.some_key')` and add the key to **both** `WEBUI/src/i18n/en.ts` and `hu.ts` (`npm run typecheck` fails if `hu.ts` is missing one). Server-sent text is translated by stable keys, falling back to the server's English when a key is missing:

| From the server | i18n key |
|---|---|
| `UiRoute` title | `nav./dns` (the route path) |
| `Module.title` | `module.dns` |
| `Permission` description | `perm.dns.review.manage` |
| widget title / hint | `widget.dns.top_blocked`, `widget.dns.top_blocked.hint` (gets `widget.data` as params) |
| `AppError` / `Problem` code | `code.dns.domain_exists` (gets `params`) |
| settings field label / description | `settings.dns.upstreams`, `settings.dns.upstreams.help` |

## 7. Tests

Add `server/tests/test_<module>.py`. The fixtures `admin`, `anon`, `login_as(...)` and `org` (fresh DB per test) are in `tests/conftest.py`.
