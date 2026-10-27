import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_container_uses_explicit_assets_and_unprivileged_single_worker():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "LIFEPLUS_ROOT=/app" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert '"--workers", "1"' in dockerfile
    assert '"--forwarded-allow-ips", "*"' not in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert "COPY . ." not in dockerfile


def test_compose_configuration_is_valid_without_real_credentials():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is not installed")
    env = {**os.environ, "SHOWCASE_PASSWORD": "test-only-not-used-for-deployment-0123456789",
           "MYSQL_PASSWORD": "test-only-mysql", "MYSQL_ROOT_PASSWORD": "test-only-root",
           "POSTGRES_PASSWORD": "test-only-postgres", "LIFEPLUS_DOMAIN": "life.example",
           "PUBLIC_ORIGIN": "https://life.example", "LIFEPLUS_ENV_FILE": ".env.example"}
    subprocess.run(["docker", "compose", "--env-file", ".env.example", "-f", "docker-compose.yml",
                    "-f", "deploy/compose.backends.yml", "-f", "deploy/compose.https.yml", "config", "--quiet"],
                   cwd=ROOT, env=env, check=True, capture_output=True, timeout=15)


@pytest.mark.asyncio
async def test_frontend_fallback_never_masks_missing_api_or_assets():
    from car_agent.api.app import app, frontend_dist
    if not frontend_dist.exists():
        pytest.skip("Build the frontend for SPA static routing acceptance")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        for path in ["/api/not-a-real-api", "/assets/missing.js", "/.env"]:
            assert (await client.get(path)).status_code == 404
        response = await client.get("/discover")
        assert response.status_code == 200 and "text/html" in response.headers["content-type"]
