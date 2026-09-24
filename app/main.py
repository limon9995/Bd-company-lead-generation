import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import config
from app.deps import LoginRequired
from app.routers import auth, campaigns, leads, outbox, public, runs, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    if not config.session_secret:
        raise RuntimeError("SESSION_SECRET is not set (see .env.example)")
    app = FastAPI(title="BD Lead Generation", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=config.session_secret, same_site="lax",
                       https_only=config.secure_cookies, max_age=7 * 24 * 3600, session_cookie="leadgen_session")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", 303)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    for r in (public, auth, runs, settings, campaigns, leads, outbox):
        app.include_router(r.router)
    return app


app = create_app()
