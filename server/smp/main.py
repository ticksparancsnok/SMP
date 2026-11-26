# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)  # ruff: ignore[missing-copyright-notice, undocumented-public-module]
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, TypedDict

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from smp import __version__
from smp.core import db
from smp.core.config import get_settings
from smp.core.context import Ctx, get_ctx
from smp.core.errors import AppError
from smp.core.events import EventBus, InProcessEventBus
from smp.core.identity import LocalIdentityProvider
from smp.core.models import Role
from smp.core.module import ModuleRegistry
from smp.core.security import SecretKeyMissing
from smp.core.throttle import LoginThrottle, seed_throttle_from_audit
from smp.modules import build_registry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from smp.core.module import ModuleRegistry

    class Healthz(TypedDict):  # ruff: ignore[undocumented-public-class]
        status: Literal["ok"]
        version: str

logger = logging.getLogger("smp")


def create_app(  # ruff: ignore[complex-structure, undocumented-public-function]
    registry: ModuleRegistry | None = None, bus: EventBus | None = None
) -> FastAPI:
    settings = get_settings()
    registry = registry or build_registry(settings.enabled_module_names())
    bus = bus or InProcessEventBus()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Fail at startup, not on the first user import hours later.
        if problem := settings.secret_key_problem():
            msg = f"refusing to start: {problem}. See .env.example."
            raise RuntimeError(msg)

        db.init_engine()

        async with db.sessionmaker()() as session:
            seeded = await seed_throttle_from_audit(app.state.login_throttle, session)  # ruff: ignore[line-too-long]
        if seeded:
            logger.info("login throttle restored from %d recent audit entries", seeded)  # ruff: ignore[line-too-long]

        for m in registry:
            await m.startup()
        logger.info("SMP %s started with modules: %s", __version__, ", ".join(m.name for m in registry))  # ruff: ignore[line-too-long]

        try:
            yield
        finally:
            for m in reversed(list(registry)):
                await m.shutdown()
            if isinstance(bus, InProcessEventBus):
                await bus.drain()
            await db.dispose_engine()

    # The interactive docs and the schema are served below, for admins only.
    app: FastAPI = FastAPI(
        title="SMP",
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        description="SMP - School Management Portal: https://github.com/ticksparancsnok/SMP",
        license_info={"name": "AGPL-3.0-or-later", "identifier": "AGPL-3.0-or-later"},  # ruff: ignore[line-too-long]
    )
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
            providers[p.source] = p  # pyright: ignore[reportArgumentType]
        if r := m.router():
            # Core routes live directly under /api/v1, module routes under /api/v1/<name>.  # ruff: ignore[line-too-long]
            prefix = "/api/v1" if m.name == "core" else f"/api/v1/{m.name}"
            app.include_router(r, prefix=prefix, tags=None if m.name == "core" else [m.name])  # ruff: ignore[line-too-long]
        if pr := m.public_router():
            app.include_router(pr, tags=[f"{m.name} (public)"])
    app.state.identity_providers = providers

    if origins := [o.strip() for o in settings.cors_origins.split(",") if o.strip()]:  # ruff: ignore[line-too-long]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"]
        )

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:  # ruff: ignore[unused-async, unused-function-argument]
        # English `detail` plus a `code` the UI translates (see smp/core/errors.py).  # ruff: ignore[line-too-long]
        return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)  # ruff: ignore[line-too-long]

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:  # ruff: ignore[line-too-long, unused-async, unused-function-argument]
        # Unique violations (duplicate username, room name, ...) -> 409 instead of 500.  # ruff: ignore[line-too-long]
        return JSONResponse(AppError(409, "conflict", "conflict with existing data").body(), status_code=409)  # ruff: ignore[line-too-long]

    @app.exception_handler(SecretKeyMissing)
    async def secret_key_missing(request: Request, exc: SecretKeyMissing) -> JSONResponse:  # ruff: ignore[line-too-long, unused-async, unused-function-argument]
        err = AppError(
            503,
            "otp.secret_key_missing",
            "server is not configured for one-time passwords: set SMP_SECRET_KEY"  # ruff: ignore[line-too-long]
            )
        return JSONResponse(err.body(), status_code=503)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> Healthz:
        return {"status": "ok", "version": __version__}

    # API documentation: a map of every endpoint and parameter, so admins only
    # (a signed-in user with the admin role; API tokens don't qualify).
    # The web UI's types come from `smp openapi`,
    # which doesn't need these routes.
    async def admin_user(ctx: Annotated[Ctx, Depends(get_ctx)]) -> Ctx:  # ruff: ignore[unused-async]
        p = ctx.principal
        if p is None or p.kind != "user" or not any(r.role == Role.ADMIN and r.room_id is None and r.building_id is None for r in p.roles):  # ruff: ignore[line-too-long]
            raise AppError(
                status.HTTP_403_FORBIDDEN,
                "docs.admin_only",
                "the API documentation is for administrators only"
            )
        return ctx

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_schema(ctx: Annotated[Ctx, Depends(admin_user)]) -> JSONResponse:  # ruff: ignore[line-too-long, unused-function-argument]
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(ctx: Annotated[Ctx, Depends(admin_user)]) -> HTMLResponse:  # ruff: ignore[line-too-long, unused-function-argument]
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="SMP API")  # ruff: ignore[line-too-long]

    if settings.ui_dir:
        _mount_ui(app, Path(settings.ui_dir))

    return app


def _mount_ui(app: FastAPI, ui: Path) -> None:
    """Serve the built single-page app. Registered last so API and public module routes (e.g. /s/{code}) win; every other GET returns index.html."""  # ruff: ignore[line-too-long]
    index = ui / "index.html"

    if not index.is_file():
        logger.warning("SMP_UI_DIR=%s has no index.html; UI not served", ui)
        return

    if (ui / "assets").is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=ui / "assets"),
            name="ui-assets"
        )

    # favicon.ico etc.
    top_level = {f.name: f for f in ui.iterdir() if f.is_file()}

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "not found")
        if path in top_level:
            return FileResponse(top_level[path])
        return FileResponse(index, headers={"cache-control": "no-cache"})
