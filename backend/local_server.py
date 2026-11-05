"""Serve the production UI and API on one loopback-only port."""

import os
import secrets
import hmac
from pathlib import Path

import uvicorn
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from fastapi import Request
from starlette.responses import JSONResponse

from local_security import LocalBrowserBoundary
from main import app

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("BACKEND_PORT", "3015"))
DIST = ROOT / "BillNote_frontend" / "dist"
STOP_TOKEN_PATH = ROOT / "backend" / ".local-stop-token"
if not STOP_TOKEN_PATH.exists():
    STOP_TOKEN_PATH.write_text(secrets.token_urlsafe(32), encoding="ascii")
STOP_TOKEN = STOP_TOKEN_PATH.read_text(encoding="ascii").strip()
server = None

if not (DIST / "index.html").is_file():
    raise RuntimeError("Production UI is missing; rebuild BillNote_frontend first.")

app.add_middleware(
    LocalBrowserBoundary,
    allowed_origins=[f"http://{host}:{PORT}" for host in ("127.0.0.1", "localhost")],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.get("/local-health", include_in_schema=False)
def local_health():
    return {"app": "BiliNote", "source": "El1mir", "deployment": "local-production"}


@app.post("/local-control/stop", include_in_schema=False)
async def local_stop(request: Request):
    if not hmac.compare_digest(request.headers.get("x-local-control-token", ""), STOP_TOKEN):
        return JSONResponse({"detail": "Local control token required."}, status_code=403)
    if server is not None:
        server.should_exit = True
    return {"stopping": True}


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            first = path.replace("\\", "/").lstrip("/").split("/", 1)[0]
            if exc.status_code != 404 or first in {"api", "static", "uploads", "assets"} or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


app.mount("/", SPAStaticFiles(directory=DIST, html=True), name="local-web")

if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, reload=False))
    server.run()
