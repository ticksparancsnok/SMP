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
        Permission("dns.lists.write", "Edit block/allow lists"),          # no default role: admin only
        # Things that happen *in a room* are room_scoped: a teacher assigned to
        # Lab 1 gets this permission only for Lab 1 (and one assigned to a building,
        # in every room of it).
        # Permission("classroom.screens.lock", "Lock screens", ("teacher",), room_scoped=True),
        # Inherently school-wide data is org_wide: every holder may use it school-wide,
        # whatever scope their role was given at.
        # Permission("shortlinks.links.create", "Create own links", ("teacher",), org_wide=True),
        # Neither flag = "building-level" (the admin-type powers): from a building-scoped
        # role it applies only inside that building (see below).
        # Permission("dns.rules.write", "Edit rules of the own building", ("manager",)),
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
        if not ctx.principal.can("dns.review.manage"):   # org-wide check; see "Dashboard widgets" below
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

### Permissions: kinds and default roles

**Kinds.** Pick one per permission; it decides how a *scoped* role assignment (a room or a building) applies it (the full table is in [architecture.md](architecture.md), "RBAC"):

| Kind | Declare | A room-scoped role gives it | A building-scoped role gives it |
|---|---|---|---|
| room-scoped | `room_scoped=True`: things that live in a room (screens, devices, room policies) | in that room | in every room of the building |
| org-wide | `org_wide=True`: inherently school-wide data (blocklists, own short links) | everywhere | everywhere |
| building-level | neither flag: administrating people, rooms, policies | everywhere (legacy teacher behaviour) | **only in that building** |

Building-level is the safe default: a manager of one building must not get a school-wide power because a permission was left unclassified. The price is that `require("perm")` (org-wide) denies them, so a handler for a building-level permission uses `any_room=True` and scopes its data (section 4). Flag `org_wide` only when the data really is the whole school's.

**Default roles.** List the *lowest* role that should have the permission; roles inherit (`smp/core/roles.py`): `manager` gets everything `teacher` has, `unit_manager` everything `manager` has, and `admin` has everything. So `("teacher",)` means teachers, managers and unit managers; `("manager",)` managers and up; `("unit_manager",)` unit managers and admins; no default role means admin only. Don't list `admin` (it always has it) or the roles above the one you mean. `student` gets nothing unless you say so (and think about it first).

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
- **Room-scoped and building-level actions** (lock screens, exam mode, a manager's rooms): use `require(perm, any_room=True)` (a grant for some room or building is enough to get in) and then check per object: `ctx.check(perm, room_id=device.room_id)` or `ctx.check(perm, building_id=thing.building_id)`. A check with no room or building (`room_id=None`) needs an org-wide grant, so things in no room or building are unit level. When something moves, check the old *and* the new place. For an object outside the caller's scope whose existence shouldn't leak, answer 404 rather than 403 (as the users and buildings endpoints do).
- **List endpoints:** filter by where the caller's permission applies. `rooms_with(perm)` and `buildings_with(perm)` return `None` for "everywhere", otherwise the set of ids (a building grant includes that building's rooms in `rooms_with`):

  ```python
  @router.get("/things")
  async def list_things(ctx: Ctx = Depends(require("mymod.things.read", any_room=True))):
      stmt = ctx.select(Thing).order_by(Thing.name)
      p = ctx.principal
      if (rooms := p.rooms_with("mymod.things.read")) is not None:       # room-scoped permission
          stmt = stmt.where(Thing.room_id.in_(rooms))
      # building-level permission, for things with an owner building:
      # if (buildings := p.buildings_with("mymod.things.manage")) is not None:
      #     stmt = stmt.where(Thing.building_id.in_(buildings))
      return list((await ctx.db.execute(stmt)).scalars())
  ```

  Forgetting this is the one way to leak between buildings, so write a test with a manager of another building (section 7).
- **Audit and buildings:** `ctx.audit(action, obj, details)` records which buildings the entry is about (`audit_log.building_ids`), and that decides which managers can read it. It works it out from `obj`: a `Building`, an object with `building_id`, or one with `room_id` (looked up inside the INSERT). Pass `building_ids=[...]` when `obj` doesn't carry it (a link table, a user's role change) or the entry concerns two buildings (a move: the old and the new). Without any building the entry is unit level only. It is set at insert time and never updated (the table is append-only).
- Read settings with `await ctx.settings("dns")`. It returns your pydantic model with defaults filled in.
- Effective policy for a device: `await effective_policies(ctx.db, ctx.org_id, device=d, kinds={"dns"})` (from `smp.core.policy`).
- **Dashboard widgets** (`Module.dashboard`) are built for every signed-in user, so they obey the same scope: use `can_anywhere(perm)` to decide whether to show a card, then restrict its counts and lists with `rooms_with` / `buildings_with` as above. A card that counts the whole school for a manager of one building leaks the other buildings' data.
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

Add `server/tests/test_<module>.py`. The fixtures `admin`, `anon`, `login_as(...)` and `org` (fresh DB per test) are in `tests/conftest.py`. For scoped behaviour, `make_building(name, rooms=[...])` creates a building with rooms and `login_scoped(app, username, roles)` logs in a user with any role assignments (e.g. `{"role": "manager", "building_id": ...}`, which the users API hands out only to some callers). Test the cases that matter: a manager of the building, a manager of *another* building (must see and change nothing), a unit manager, and a teacher assigned to a room.
