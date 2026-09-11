"""The control panel: a small FastAPI app around the worker."""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import __version__
from .doctor import run_checks
from .logbuf import RING

log = logging.getLogger(__name__)

PANEL_HTML = Path(__file__).with_name("panel.html")

security = HTTPBasic(auto_error=False)


def create_app(context) -> FastAPI:
    """`context` carries the live objects: settings, stats, worker, client, gate…"""
    settings = context.settings
    app = FastAPI(title="Content Extractor", docs_url=None, redoc_url=None,
                  openapi_url=None, version=__version__)

    def authorise(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
        expected_user = settings.panel_user or "admin"
        expected_password = settings.panel_password
        if not expected_password:
            # A panel with no password is never exposed - fail closed.
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "PANEL_PASSWORD is not set")
        if credentials is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required",
                                {"WWW-Authenticate": "Basic realm=content-extractor"})
        user_ok = secrets.compare_digest(credentials.username, expected_user)
        password_ok = secrets.compare_digest(credentials.password, expected_password)
        if not (user_ok and password_ok):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password",
                                {"WWW-Authenticate": "Basic realm=content-extractor"})
        return credentials.username

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def index(_: str = Depends(authorise)) -> HTMLResponse:
        try:
            return HTMLResponse(PANEL_HTML.read_text(encoding="utf-8"))
        except OSError as exc:
            log.error("Cannot read panel.html: %s", exc)
            return HTMLResponse("<h1>panel.html is missing</h1>", status_code=500)

    @app.get("/healthz")
    def healthz() -> dict:
        """Unauthenticated liveness probe - used by install.sh and systemd."""
        return {
            "ok": True,
            "version": __version__,
            "state": context.stats.state,
            "node_name": settings.node_name,
        }

    # ---------------------------------------------------------------- data
    @app.get("/api/stats")
    def api_stats(_: str = Depends(authorise)) -> dict:
        data = context.stats.snapshot()
        data.update({
            "version": __version__,
            "node_name": settings.node_name,
            "worker": {
                "running": context.worker.running,
                "paused": context.worker.paused,
            },
            "settings": settings.public(),
            "n8n": context.client.snapshot(),
            "renderer": context.renderer.snapshot(),
            "domains": context.gate.snapshot(),
        })
        return data

    @app.get("/api/logs")
    def api_logs(n: int = 200, _: str = Depends(authorise)) -> dict:
        return {"lines": RING.tail(max(1, min(n, 1000)))}

    @app.get("/api/doctor")
    def api_doctor(_: str = Depends(authorise)) -> dict:
        checks = run_checks(settings, include_network=True)
        return {"checks": [check.as_dict() for check in checks]}

    # ---------------------------------------------------------------- actions
    @app.post("/api/control")
    def api_control(body: dict, _: str = Depends(authorise)) -> JSONResponse:
        action = str(body.get("action", "")).lower()
        if action == "start":
            started = context.worker.start()
            message = "Worker started" if started else (
                context.stats.state_detail or "Worker is already running")
            return JSONResponse({"ok": started, "message": message})
        if action == "stop":
            context.worker.stop()
            return JSONResponse({"ok": True, "message": "Worker stopped"})
        if action == "pause":
            context.worker.pause()
            return JSONResponse({"ok": True, "message": "Worker paused"})
        if action == "resume":
            context.worker.resume()
            return JSONResponse({"ok": True, "message": "Worker resumed"})
        if action == "reset_cooloffs":
            count = context.gate.reset_cooloffs()
            return JSONResponse({"ok": True, "message": f"Cleared {count} cool-off(s)"})
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown action: {action!r}")

    @app.post("/api/settings")
    def api_settings(body: dict, _: str = Depends(authorise)) -> dict:
        applied, rejected = settings.apply(body or {})
        if "threads" in applied:
            context.worker.limiter.set_limit(settings.threads)
        if applied:
            log.info("Settings changed from the panel: %s", applied)
        return {
            "ok": not rejected,
            "applied": applied,
            "rejected": rejected,
            "settings": settings.public(),
            "note": "Runtime only - edit the config file to make changes permanent",
        }

    @app.post("/api/test")
    def api_test(body: dict, _: str = Depends(authorise)) -> dict:
        url = str(body.get("url", "")).strip()
        if not url:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No url given")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        return context.worker.test_url(url)

    return app
