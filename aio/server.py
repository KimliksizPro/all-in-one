"""Yerel FastAPI sunucusu ve web arayüzü API'si.

Yalnızca 127.0.0.1 dinler. Yerel olmayan Origin/Host istekleri reddedilir;
token'lar hiçbir API yanıtında yer almaz.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, antigravity
from .accounts import AccountConflict, AccountError, AppRunningError, NoLiveAccountError
from .keyring import KeyringError
from .probe import ProbeError
from .quota import QuotaError
from .service import Service
from .util import redact

log = logging.getLogger("aio.server")
WEB_DIR = Path(__file__).parent / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


# ---------- istek modelleri ----------


class CaptureRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    label: str | None = None
    overwrite: bool = False
    primary: bool = False


class SwitchRequest(BaseModel):
    stop: bool = False
    launch: bool | None = None
    verify: bool = True
    force: bool = False


class RenameRequest(BaseModel):
    new_name: str = Field(min_length=1, max_length=64)


class RefreshRequest(BaseModel):
    all: bool = True
    name: str | None = None
    force: bool = True


class AppStopRequest(BaseModel):
    force: bool = False


class SettingsUpdate(BaseModel):
    warn_percent: float | None = None
    critical_percent: float | None = None
    poll_seconds: int | None = None
    desktop_notifications: bool | None = None
    launch_after_switch: bool | None = None
    auto_refresh_all: bool | None = None
    all_poll_seconds: int | None = None


# ---------- güvenlik ----------


def _hostname(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _origin_allowed(origin: str) -> bool:
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    return _hostname(parts.hostname or "") in ALLOWED_HOSTS


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    payload = {"detail": message}
    payload.update(extra)
    return JSONResponse(status_code=status, content=payload)


def create_app(service: Service | None = None, *, start_poller: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or Service()
        app.state.service = svc
        try:
            await asyncio.to_thread(svc.bootstrap)
        except Exception as exc:
            log.warning("bootstrap: %s", redact(exc))
        task: asyncio.Task | None = None
        if start_poller:
            task = asyncio.create_task(_poll_loop(svc), name="aio-poll")
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    app = FastAPI(title="all-in-one", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.service_override = service

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        host = _hostname(request.headers.get("host", ""))
        if host and host not in ALLOWED_HOSTS:
            return _error(403, "Yerel olmayan Host başlığı reddedildi.")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and not _origin_allowed(origin):
                return _error(403, "Yerel olmayan Origin reddedildi.")
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return _error(403, "Cross-site istek reddedildi.")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    # ---------- exception handler'ları ----------

    @app.exception_handler(AccountConflict)
    async def _account_conflict(_request: Request, exc: AccountConflict):
        return _error(409, str(exc), existing=exc.existing)

    @app.exception_handler(AppRunningError)
    async def _app_running(_request: Request, exc: AppRunningError):
        return _error(409, str(exc), needs_stop=True)

    @app.exception_handler(NoLiveAccountError)
    async def _no_live(_request: Request, exc: NoLiveAccountError):
        return _error(409, str(exc))

    @app.exception_handler(AccountError)
    async def _account_error(_request: Request, exc: AccountError):
        return _error(400, str(exc))

    @app.exception_handler(QuotaError)
    async def _quota_error(_request: Request, exc: QuotaError):
        return _error(409 if exc.needs_stop else 502, str(exc), needs_stop=exc.needs_stop)

    @app.exception_handler(ProbeError)
    async def _probe_error(_request: Request, exc: ProbeError):
        return _error(502, redact(exc))

    @app.exception_handler(KeyringError)
    async def _keyring_error(_request: Request, exc: KeyringError):
        return _error(503, redact(exc))

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        log.exception("unhandled: %s", redact(exc))
        return _error(500, "Beklenmeyen sunucu hatası.", detail_safe=redact(exc))

    # ---------- yardımcı ----------

    def svc(request: Request) -> Service:
        return request.app.state.service

    # ---------- statik ----------

    if (WEB_DIR / "static").is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        idx = WEB_DIR / "index.html"
        if not idx.is_file():
            return HTMLResponse("<h1>UI bulunamadı.</h1>", status_code=500)
        return HTMLResponse(idx.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "version": __version__, "ts": int(time.time())}

    # ---------- durum ----------

    @app.get("/api/state")
    async def state(request: Request) -> dict:
        return await asyncio.to_thread(svc(request).snapshot)

    @app.get("/api/doctor")
    async def doctor(request: Request, probe: bool = False) -> dict:
        return await asyncio.to_thread(svc(request).doctor, probe=probe)

    # ---------- hesap işlemleri ----------

    @app.post("/api/accounts/capture")
    async def capture(request: Request, body: CaptureRequest) -> dict:
        result = await asyncio.to_thread(
            svc(request).capture,
            body.name,
            body.label,
            overwrite=body.overwrite,
            primary=body.primary,
        )
        return {"ok": True, **result.to_dict(), "state": await asyncio.to_thread(svc(request).snapshot)}

    @app.post("/api/accounts/{name}/switch")
    async def switch(request: Request, name: str, body: SwitchRequest) -> dict:
        result = await asyncio.to_thread(
            svc(request).switch,
            name,
            stop=body.stop,
            launch=body.launch,
            validate=body.verify,
        )
        return {"ok": True, **result.to_dict(), "state": await asyncio.to_thread(svc(request).snapshot)}

    @app.post("/api/accounts/{name}/rename")
    async def rename(request: Request, name: str, body: RenameRequest) -> dict:
        meta = await asyncio.to_thread(svc(request).rename, name, body.new_name)
        return {"ok": True, "name": meta.name, "state": await asyncio.to_thread(svc(request).snapshot)}

    @app.post("/api/accounts/{name}/delete")
    async def delete(request: Request, name: str) -> dict:
        await asyncio.to_thread(svc(request).delete, name)
        return {"ok": True, "state": await asyncio.to_thread(svc(request).snapshot)}

    @app.post("/api/accounts/{name}/primary")
    async def primary(request: Request, name: str) -> dict:
        meta = await asyncio.to_thread(svc(request).set_primary, name)
        return {"ok": True, "primary": meta.name, "state": await asyncio.to_thread(svc(request).snapshot)}

    @app.post("/api/accounts/login/start")
    async def login_start(request: Request) -> dict:
        state = await asyncio.to_thread(svc(request).start_login)
        return {"ok": True, "login": state, "state": await asyncio.to_thread(svc(request).snapshot)}

    @app.post("/api/accounts/login/reset")
    async def login_reset(request: Request) -> dict:
        state = await asyncio.to_thread(svc(request).reset_login)
        return {"ok": True, "login": state, "state": await asyncio.to_thread(svc(request).snapshot)}

    # ---------- kota ----------

    @app.post("/api/quota/refresh")
    async def refresh(request: Request, body: RefreshRequest) -> dict:
        s = svc(request)
        if body.name:
            quota = await asyncio.to_thread(s.refresh_one, body.name, force=body.force)
            return {"ok": True, "quota": quota.to_dict(), "state": await asyncio.to_thread(s.snapshot)}
        job = await asyncio.to_thread(s.start_refresh_all)
        return {"ok": True, "job": job, "state": await asyncio.to_thread(s.snapshot)}

    @app.get("/api/quota/refresh")
    async def refresh_status(request: Request) -> dict:
        s = svc(request)
        return {"job": s.quota.job_state(), "state": await asyncio.to_thread(s.snapshot)}

    # ---------- uygulama ----------

    @app.post("/api/app/start")
    async def app_start(request: Request) -> dict:
        if await asyncio.to_thread(antigravity.running):
            return {"ok": True, "already_running": True, "state": await asyncio.to_thread(svc(request).snapshot)}
        pid = await asyncio.to_thread(antigravity.start)
        return {"ok": True, "pid": pid, "state": await asyncio.to_thread(svc(request).snapshot)}

    @app.post("/api/app/stop")
    async def app_stop(request: Request, body: AppStopRequest) -> dict:
        result = await asyncio.to_thread(antigravity.stop, 12.0, body.force)
        if result.remaining:
            raise AppRunningError("Antigravity kapanmadı. Çalışan PID: " + ", ".join(map(str, result.remaining)))
        return {"ok": True, **result.to_dict(), "state": await asyncio.to_thread(svc(request).snapshot)}

    # ---------- ayarlar ----------

    @app.put("/api/settings")
    async def update_settings(request: Request, body: SettingsUpdate) -> dict:
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        settings = await asyncio.to_thread(svc(request).update_settings, updates)
        return {"ok": True, "settings": settings.__dict__, "state": await asyncio.to_thread(svc(request).snapshot)}

    return app


async def _poll_loop(service: Service) -> None:
    """Aktif hesabı periyodik tazele; eşik bildirimlerini tetikle."""
    await asyncio.sleep(1.0)
    last_all = 0.0
    while True:
        try:
            settings = service.accounts.state().settings
            if not service.quota.job_state().get("running"):
                try:
                    await asyncio.to_thread(service.refresh_active, force=False)
                except Exception as exc:
                    log.debug("active poll: %s", redact(exc))
                try:
                    await asyncio.to_thread(service.evaluate_alerts, notify=True)
                except Exception as exc:
                    log.debug("alert evaluate: %s", redact(exc))
                if (
                    settings.auto_refresh_all
                    and not antigravity.running()
                    and len(service.accounts.state().profiles) > 1
                    and time.monotonic() - last_all >= settings.all_poll_seconds
                ):
                    try:
                        await asyncio.to_thread(service.start_refresh_all)
                        last_all = time.monotonic()
                    except Exception as exc:
                        log.debug("all poll: %s", redact(exc))
            delay = max(30, int(settings.poll_seconds))
        except Exception as exc:
            log.debug("poll settings: %s", redact(exc))
            delay = 300
        await asyncio.sleep(delay)


app = create_app()
