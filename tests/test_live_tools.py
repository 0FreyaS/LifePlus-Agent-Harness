import os

import pytest

from car_agent.tool_smoke import run_checks


@pytest.mark.skipif(os.getenv("LIFEPLUS_LIVE_TESTS") != "1", reason="Set LIFEPLUS_LIVE_TESTS=1 for real upstream calls")
@pytest.mark.asyncio
async def test_all_tools_over_real_mcp():
    report = await run_checks()
    failed = [case for case in report["cases"] if not case["passed"]]
    assert not failed, failed
