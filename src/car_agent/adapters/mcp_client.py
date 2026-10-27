from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class _Call:
    name: str
    arguments: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]


class LocalMCPClient:
    """Persistent stdio MCP connection whose AnyIO contexts have one task owner."""

    def __init__(self, env: dict[str, str] | None = None):
        self.session: ClientSession | None = None
        self.tool_names: set[str] = set()
        self.env = env if env is not None else dict(os.environ)
        self._requests: asyncio.Queue[_Call | None] = asyncio.Queue()
        self._owner: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._inflight: set[asyncio.Task] = set()

    async def open(self) -> None:
        if self._owner and not self._owner.done():
            await self._ready.wait()
        else:
            self._ready = asyncio.Event()
            self._startup_error = None
            self._owner = asyncio.create_task(self._serve(), name="lifeplus-mcp-owner")
            await self._ready.wait()
        if self._startup_error:
            raise RuntimeError(f"MCP 服务启动失败：{self._startup_error}") from self._startup_error
        if not self.session:
            raise RuntimeError("MCP 会话未就绪")

    async def call(self, name: str, arguments: dict) -> dict:
        if not self.session:
            await self.open()
        if name not in self.tool_names:
            raise RuntimeError(f"MCP tool unavailable: {name}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        await self._requests.put(_Call(name, arguments, future))
        try:
            return await asyncio.wait_for(future, timeout=20)
        finally:
            if not future.done():
                future.cancel()

    async def close(self) -> None:
        owner = self._owner
        if not owner:
            return
        if not owner.done():
            await self._requests.put(None)
        await asyncio.gather(owner, return_exceptions=True)
        self._owner = None
        self.session = None
        self.tool_names.clear()

    async def _serve(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "car_agent.mcp_server"],
            env=self.env,
        )
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self.session = session
                    self.tool_names = {item.name for item in listing.tools}
                    self._ready.set()
                    while True:
                        request = await self._requests.get()
                        if request is None:
                            break
                        if request.future.cancelled():
                            continue
                        task = asyncio.create_task(self._deliver(session, request))
                        self._inflight.add(task)
                        task.add_done_callback(self._inflight.discard)
                    for task in self._inflight:
                        task.cancel()
                    await asyncio.gather(*self._inflight, return_exceptions=True)
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc
                self._ready.set()
            await self._fail_pending(exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.session = None
            self.tool_names.clear()
            if not self._ready.is_set():
                self._ready.set()

    async def _deliver(self, session: ClientSession, request: _Call) -> None:
        invocation = asyncio.create_task(self._invoke(session, request.name, request.arguments))

        def cancel_invocation(future: asyncio.Future) -> None:
            if future.cancelled():
                invocation.cancel()

        request.future.add_done_callback(cancel_invocation)
        try:
            result = await asyncio.wait_for(invocation, timeout=18)
            if not request.future.done():
                request.future.set_result(result)
        except asyncio.CancelledError:
            invocation.cancel()
            request.future.cancel()
            raise
        except Exception as exc:
            if not request.future.done():
                request.future.set_exception(exc)
        finally:
            request.future.remove_done_callback(cancel_invocation)

    async def _fail_pending(self, exc: BaseException) -> None:
        while not self._requests.empty():
            request = self._requests.get_nowait()
            if request is not None and not request.future.done():
                request.future.set_exception(exc)

    @staticmethod
    async def _invoke(session: ClientSession, name: str, arguments: dict) -> dict:
        result = await session.call_tool(name, arguments)
        if result.isError:
            raise RuntimeError("MCP tool returned an error")
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            if not isinstance(structured, dict):
                raise TypeError("MCP structuredContent 必须为对象")
            return structured
        contents = [getattr(part, "text", "") for part in result.content]
        for content in contents:
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    return parsed
                if isinstance(parsed, list):
                    return {"items": parsed}
            except (TypeError, json.JSONDecodeError):
                continue
        raise ValueError("MCP 工具未返回有效的结构化 JSON")
