from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

from local_security import LocalBrowserBoundary


def make_client():
    app = FastAPI()
    app.add_middleware(LocalBrowserBoundary, allowed_origins=["http://127.0.0.1:3015", "http://localhost:3015"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.get("/api/check")
    def check():
        return {"ok": True}

    return TestClient(app, base_url="http://127.0.0.1:3015")


def test_local_cli_and_browser_are_allowed():
    client = make_client()
    assert client.get("/api/check").json() == {"ok": True}
    assert client.get("/api/check", headers={"Origin": "http://localhost:3015", "Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_foreign_and_null_origins_are_rejected():
    client = make_client()
    for origin in ("https://untrusted.example", "null", "http://localhost:9090"):
        assert client.get("/api/check", headers={"Origin": origin}).status_code == 403


def test_cross_site_get_without_origin_is_rejected():
    assert make_client().get("/api/check", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_dns_rebinding_host_is_rejected():
    assert make_client().get("/api/check", headers={"Host": "untrusted.example"}).status_code == 400
