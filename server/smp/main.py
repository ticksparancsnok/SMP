import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from smp import __version__
from smp.core import db
from smp.core.config import get_settings
from smp.core.context import Ctx, get_ctx
from smp.core.errors import AppError
from smp.core.events import EventBus, InProcessEventBus
from smp.core.identity import LocalIdentityProvider
from smp.core.module import ModuleRegistry
from smp.core.security import SecretKeyMissing
from smp.core.throttle import LoginThrottle, seed_throttle_from_audit
from smp.modules import build_registry

log = logging.getLogger("smp")


def create_app(registry: ModuleRegistry | None = None, bus: EventBus | None = None) -> FastAPI:
    settings = get_settings()
    registry = registry or build_registry(settings.enabled_module_names())
    bus = bus or InProcessEventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Fail at startup, not on the first user import hours later.
        if problem := settings.secret_key_problem():
            raise RuntimeError(f"refusing to start: {problem}. See .env.example.")
        db.init_engine()
        async with db.sessionmaker()() as session:
            seeded = await seed_throttle_from_audit(app.state.login_throttle, session)
        if seeded:
            log.info("login throttle restored from %d recent audit entries", seeded)
        for m in registry:
            await m.startup()
        log.info("SMP %s started with modules: %s", __version__, ", ".join(m.name for m in registry))
        yield
        for m in reversed(list(registry)):
            await m.shutdown()
        if isinstance(bus, InProcessEventBus):
            await bus.drain()
        await db.dispose_engine()

    # The interactive docs and the schema are served below, for admins only.
    app = FastAPI(title="SMP", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry
    app.state.bus = bus
    app.state.login_throttle = LoginThrottle(
        max_failures=settings.login_max_failures,
        lockout_seconds=settings.login_lockout_seconds,
        ip_per_minute=settings.login_ip_attempts_per_minute,
    )

    providers = {LocalIdentityProvider.source: LocalIdentityProvider()}
    for m in registry:
        m.subscribe(bus)
        for p in m.identity_providers():
            providers[p.source] = p
        if r := m.router():
            # Core routes live directly under /api/v1, module routes under /api/v1/<name>.
            prefix = "/api/v1" if m.name == "core" else f"/api/v1/{m.name}"
            app.include_router(r, prefix=prefix, tags=None if m.name == "core" else [m.name])
        if pr := m.public_router():
            app.include_router(pr, tags=[f"{m.name} (public)"])
    app.state.identity_providers = providers

    if origins := [o.strip() for o in settings.cors_origins.split(",") if o.strip()]:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        # English `detail` plus a `code` the UI translates (see smp/core/errors.py).
        return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError):
        # Unique violations (duplicate username, room name, ...) -> 409 instead of 500.
        return JSONResponse(AppError(409, "conflict", "conflict with existing data").body(), status_code=409)

    @app.exception_handler(SecretKeyMissing)
    async def secret_key_missing(request: Request, exc: SecretKeyMissing):
        err = AppError(503, "otp.secret_key_missing", "server is not configured for one-time passwords: set SMP_SECRET_KEY")
        return JSONResponse(err.body(), status_code=503)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "version": __version__}

    # API documentation: a map of every endpoint and parameter, so admins only
    # (a signed-in user with the admin role; API tokens don't qualify). The web UI's
    # types come from `smp openapi`, which doesn't need these routes.
    async def admin_user(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        p = ctx.principal
        if p is None or p.kind != "user" or not any(role == "admin" for role, _ in p.roles):
            raise AppError(status.HTTP_403_FORBIDDEN, "docs.admin_only", "the API documentation is for administrators only")
        return ctx

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_schema(ctx: Ctx = Depends(admin_user)):
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(ctx: Ctx = Depends(admin_user)):
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="SMP API")

    if settings.ui_dir:
        _mount_ui(app, Path(settings.ui_dir))

    return app


def _mount_ui(app: FastAPI, ui: Path) -> None:
    """Serve the built single-page app. Registered last so API and public module
    routes (e.g. /s/{code}) win; every other GET returns index.html."""
    index = ui / "index.html"
    if not index.is_file():
        log.warning("SMP_UI_DIR=%s has no index.html; UI not served", ui)
        return
    if (ui / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=ui / "assets"), name="ui-assets")
    top_level = {f.name: f for f in ui.iterdir() if f.is_file()}  # favicon.ico etc.

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "not found")
        if path in top_level:
            return FileResponse(top_level[path])
        return FileResponse(index, headers={"cache-control": "no-cache"})
