import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import SecretStr

from car_agent.access import ShowcaseAccess, showcase_owner, validate_showcase
from car_agent.config import Settings


PASSWORD = "test-only-password-0123456789"


def configured(**values):
    return Settings(_env_file=None, app_mode="showcase", showcase_password=PASSWORD, **values)


def application(settings):
    app = FastAPI()
    app.add_middleware(ShowcaseAccess, settings=settings)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def page(request: Request):
        return {"owner": getattr(request.state, "showcase_owner", None)}

    return app


@pytest.mark.asyncio
async def test_gate_protects_html_assets_docs_and_api_but_exposes_only_liveness():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(configured())), base_url="https://life.example") as client:
        for path in ["/", "/assets/app.js", "/docs", "/api/health", "/api/tools", "/api/sessions"]:
            denied = await client.get(path)
            assert denied.status_code == 401 and "Basic" in denied.headers["www-authenticate"]
        assert (await client.get("/healthz")).status_code == 200
        response = await client.get("/api/sessions", auth=("interviewer", PASSWORD), headers={"X-Demo-User": "victim"})
        assert response.status_code == 200 and response.json()["owner"] == showcase_owner("interviewer")


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["Bearer invalid", "Basic !!!!", "Basic /w==", "Basic dXNlcg==", "basic "])
async def test_malformed_credentials_do_not_raise_or_pass(authorization):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(configured())), base_url="https://life.example") as client:
        assert (await client.get("/", headers={"Authorization": authorization})).status_code == 401


@pytest.mark.asyncio
async def test_https_and_same_origin_writes_are_required():
    app = application(configured(public_origin="https://life.example"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://life.example", auth=("interviewer", PASSWORD)) as client:
        assert (await client.get("/")).status_code == 426
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://life.example", auth=("interviewer", PASSWORD)) as client:
        assert (await client.post("/api/sessions", headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.post("/api/sessions", headers={"Origin": "https://life.example"})).status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", auth=("interviewer", PASSWORD)) as client:
        assert (await client.get("/")).status_code == 426
        assert (await client.get("/", headers={"Host": "localhost", "X-Forwarded-Proto": "https"})).status_code == 426
    local_app = application(configured(showcase_allow_http=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=local_app), base_url="http://127.0.0.1", auth=("interviewer", PASSWORD)) as client:
        assert (await client.get("/")).status_code == 200


@pytest.mark.asyncio
async def test_invalid_credentials_are_throttled():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(configured())), base_url="https://life.example") as client:
        for _ in range(10):
            assert (await client.get("/", auth=("interviewer", "wrong"))).status_code == 401
        response = await client.get("/", auth=("interviewer", "wrong"))
        assert response.status_code == 429 and int(response.headers["retry-after"]) > 0


def test_showcase_fails_closed_and_does_not_enable_general_production():
    for password in [None, SecretStr("short")]:
        settings = Settings(_env_file=None, app_mode="showcase", showcase_password=password)
        with pytest.raises(ValueError):
            validate_showcase(settings)
    with pytest.raises(ValueError):
        validate_showcase(configured(public_origin="http://life.example"))
    validate_showcase(Settings(_env_file=None, app_mode="production"))


@pytest.mark.asyncio
async def test_real_api_ignores_spoofed_demo_identity(tmp_path, monkeypatch):
    from car_agent.api.app import app, settings
    monkeypatch.setattr(settings, "app_mode", "showcase")
    monkeypatch.setattr(settings, "showcase_password", SecretStr(PASSWORD))
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "showcase.sqlite3"))
    monkeypatch.setattr(settings, "chroma_path", str(tmp_path / "chroma"))
    monkeypatch.setattr(settings, "public_origin", "")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://life.example", auth=(settings.showcase_username, PASSWORD)) as client:
            response = await client.post("/api/sessions", headers={"X-Demo-User": "untrusted-identity"})
            assert response.status_code == 200
            row = await app.state.db.fetchone("SELECT user_id FROM sessions WHERE id=?", (response.json()["id"],))
            assert row["user_id"] == showcase_owner(settings.showcase_username)
