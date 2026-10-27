import argparse
import asyncio
import sys

import httpx


async def chat(base_url: str, user: str) -> None:
    headers = {"X-Demo-User": user}
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=30) as client:
        response = await client.post("/api/sessions")
        response.raise_for_status()
        session_id = response.json()["id"]
        print(f"LifePlus CLI · session {session_id[:8]} · 输入 /quit 退出")
        while True:
            try:
                query = await asyncio.to_thread(input, "你> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if query.strip().lower() in {"/quit", "/exit"}:
                return
            if not query.strip():
                continue
            response = await client.post(f"/api/sessions/{session_id}/messages",
                                         json={"message": query, "mode": "live"})
            if response.is_error:
                print(f"请求失败：{response.text}")
                continue
            task_id = response.json()["task_id"]
            cursor = 0
            while True:
                events = await client.get(f"/api/tasks/{task_id}/trace")
                events.raise_for_status()
                for event in events.json():
                    if event["seq"] <= cursor:
                        continue
                    cursor = event["seq"]
                    kind = event["type"]
                    if kind == "node.started":
                        print(f"  · {event.get('node', 'node')}")
                    elif kind == "tool.started":
                        print(f"  · 调用工具 {event.get('tool_name')}")
                    elif kind == "interrupt.required":
                        print(f"  · 等待人工输入：{event.get('question', '')}")
                    elif kind == "answer.completed" and event.get("answer"):
                        print(f"Agent> {event['answer']}")
                task = await client.get(f"/api/tasks/{task_id}")
                task.raise_for_status()
                status = task.json()["status"]
                if status in {"completed", "failed", "cancelled", "waiting_user", "interrupted"}:
                    if status == "failed":
                        print(f"Agent 执行失败：{task.json().get('answer') or '请查看 Web 执行链路'}")
                    elif status == "waiting_user":
                        print(f"需要人工处理。请在 Web 工作台打开会话 {session_id[:8]} 完成确认/补充。")
                    break
                await asyncio.sleep(0.4)


def main() -> None:
    parser = argparse.ArgumentParser(description="LifePlus Agent Harness CLI")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="FastAPI base URL")
    parser.add_argument("--user", default="demo-user", help="Demo user identity")
    args = parser.parse_args()
    try:
        asyncio.run(chat(args.url, args.user))
    except httpx.HTTPError as exc:
        print(f"无法连接 LifePlus API：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
