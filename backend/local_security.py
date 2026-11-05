"""Browser boundaries for this loopback-only, single-user deployment."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class LocalBrowserBoundary(BaseHTTPMiddleware):
    def __init__(self, app, allowed_origins):
        super().__init__(app)
        self.allowed_origins = frozenset(allowed_origins)

    async def dispatch(self, request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            return JSONResponse({"detail": "This local installation rejects this origin."}, status_code=403)
        if (
            origin is None
            and request.url.path.startswith("/api")
            and request.headers.get("sec-fetch-site") == "cross-site"
        ):
            return JSONResponse({"detail": "Cross-site API requests are disabled."}, status_code=403)
        response = await call_next(request)
        response.headers["X-BiliNote-Local"] = "1"
        return response
