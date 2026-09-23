"""DARAI backend: FastAPI application.

Routes have no /api prefix; the frontend proxy strips it (docs/API_CONTRACT.md).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from . import ai_gateway
from .auth import bootstrap_admin
from .config import get_settings
from .db import db_session, get_engine
from .errors import install_error_handlers
from .notifications import run_reminders
from .processing import recover_interrupted
from .routers import auth_users, employees, meetings, notifications, system, tasks

log = logging.getLogger("darai")


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def _reminder_tick() -> int:
    settings = get_settings()
    with db_session() as db:
        return run_reminders(db, datetime.now(timezone.utc), settings.reminder_lead_hours)


async def reminder_loop(stop: asyncio.Event) -> None:
    """Single in-process scheduler (MVP: one backend process). The first tick
    runs at startup and catches events missed while the backend was down."""
    interval = get_settings().reminder_interval_seconds
    while not stop.is_set():
        try:
            created = await run_in_threadpool(_reminder_tick)
            if created:
                log.info("reminders created: %d", created)
        except Exception as exc:
            log.error("reminder tick failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def startup() -> None:
    settings = get_settings()
    settings.recordings_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    if settings.run_migrations_on_start:
        run_migrations()
    with db_session() as db:
        if bootstrap_admin(db):
            log.info("bootstrap admin created")
        interrupted = recover_interrupted(db)
        if interrupted:
            log.warning("recordings marked INTERRUPTED after restart: %d", interrupted)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await run_in_threadpool(startup)
    if settings.preload_models:
        threading.Thread(target=ai_gateway.preload, name="model-preload", daemon=True).start()
    stop = asyncio.Event()
    task = asyncio.create_task(reminder_loop(stop)) if settings.reminders_enabled else None
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            await task


def create_app() -> FastAPI:
    logging.basicConfig(level=get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = FastAPI(title="DARAI API", version="1.0.0", lifespan=lifespan,
                  description="Локальная CRM совещаний. Контракт: docs/API_CONTRACT.md. "
                              "Маршруты без префикса /api — его удаляет прокси frontend.")
    install_error_handlers(app)
    for r in (system.router, auth_users.router, auth_users.users, employees.router, meetings.router,
              tasks.router, notifications.router):
        app.include_router(r)
    return app


app = create_app()
