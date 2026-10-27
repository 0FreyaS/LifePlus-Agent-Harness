"""Run an isolated container acceptance check without provider keys or personal data."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import httpx


def check(image: str, port: int) -> dict:
    name = "lifeplus-check-" + secrets.token_hex(4)
    password = secrets.token_urlsafe(32)
    environment = {**os.environ, "SHOWCASE_PASSWORD": password}
    subprocess.run(["docker", "run", "--detach", "--rm", "--name", name, "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "128",
                    "--tmpfs", "/tmp:rw,size=128m,mode=1777", "--mount", "type=volume,destination=/app/data",
                    "--publish", f"127.0.0.1:{port}:8000", "--env", "APP_MODE=showcase",
                    "--env", "SHOWCASE_ALLOW_HTTP=true", "--env", "SHOWCASE_USERNAME=interviewer",
                    "--env", "SHOWCASE_PASSWORD", image], check=True, env=environment, capture_output=True)
    checks = {}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=8) as client:
            deadline = time.monotonic() + 90
            while True:
                try:
                    if client.get("/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Container did not become ready in 90 seconds; inspect container logs privately")
                time.sleep(0.5)
            checks["unauthenticated_html_denied"] = client.get("/").status_code == 401
            checks["unauthenticated_api_denied"] = client.get("/api/sessions").status_code == 401
            client.auth = ("interviewer", password)
            response = client.get("/")
            checks["frontend_served"] = response.status_code == 200 and '<div id="root">' in response.text
            checks["spa_deep_link"] = client.get("/discover").status_code == 200
            checks["missing_api_is_404"] = client.get("/api/not-a-real-route").status_code == 404
            checks["missing_asset_is_404"] = client.get("/assets/missing.js").status_code == 404
            health = client.get("/api/health").json()
            checks["showcase_active"] = health["auth_mode"] == "showcase"
            checks["no_provider_key_baked_in"] = not health["model_configured"] and not health["maps_configured"]
            checks["chroma_loaded"] = health["rag_backend"] == "chroma"
            tools = client.get("/api/tools")
            checks["tool_catalog_loaded"] = tools.status_code == 200 and bool(tools.json())
            session = client.post("/api/sessions").json()["id"]
            result = client.post(f"/api/sessions/{session}/messages", json={"message": "无锡今天天气怎么样", "mode": "demo"})
            result.raise_for_status()
            task = result.json()["task_id"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = client.get(f"/api/tasks/{task}").json()["status"]
                if status in {"completed", "failed", "partial"}:
                    break
                time.sleep(0.1)
            checks["isolated_demo_task_completed"] = status == "completed"
            stream = client.get(f"/api/tasks/{task}/events?after_seq=0")
            checks["sse_response"] = stream.status_code == 200 and stream.headers.get("content-type", "").startswith("text/event-stream") and "data:" in stream.text
        resources = subprocess.run(["docker", "exec", name, "python", "-c",
            "import json,os; from car_agent.api.app import root,frontend_dist; "
            "print(json.dumps({'root':str(root),'frontend':(frontend_dist/'index.html').is_file(),"
            "'skills':len(list((root/'skills').glob('*/SKILL.md'))),'knowledge':len(list((root/'knowledge').glob('*.md'))),"
            "'uid':os.getuid(),'env_file_exists':(root/'.env').exists()}))"], check=True, capture_output=True, text=True)
        paths = json.loads(resources.stdout)
        checks["packaged_resources"] = paths["root"] == "/app" and paths["frontend"] and paths["skills"] >= 7 and paths["knowledge"] >= 1
        checks["non_root"] = paths["uid"] == 10001
        checks["no_env_file_in_image"] = not paths["env_file_exists"]
        return {"passed": all(checks.values()), "checks": checks, "image": image,
                "scope": "real Docker container; isolated demo task; no live providers; no browser screenshot",
                "public_deployment": False}
    finally:
        subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="lifeplus-agent:local")
    parser.add_argument("--port", type=int, default=18088)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path")
    result = check(args.image, args.port)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
