import logging
import os

from mcp.server.fastmcp import FastMCP

from .tools import LifeTools

# AMap carries credentials in query parameters. Do not log upstream URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

mcp = FastMCP("lifeplus-services")
services = LifeTools(os.getenv("LIFEPLUS_DATA_PATH", "./data/lifeplus.sqlite3"),
                     os.getenv("AMAP_MAPS_API_KEY"))


@mcp.tool()
async def maps_weather(city: str, forecast_date: str = "") -> dict:
    """查询高德真实城市天气；forecast_date 为 YYYY-MM-DD，默认今天。"""
    return await services.call("maps_weather", locals())


@mcp.tool()
async def maps_search(keywords: str, city: str = "") -> dict:
    """搜索高德真实地点；返回候选 ID、名称、地址与经纬度，不自动替用户选择。"""
    return await services.call("maps_search", locals())


@mcp.tool()
async def maps_route(origin: str, destination: str, city: str = "", travel_mode: str = "driving") -> dict:
    """真实路线规划；支持 driving/walking/bicycling，地点不明确时返回澄清信息。"""
    return await services.call("maps_route", locals())


@mcp.tool()
async def maps_local_search(category: str, anchor: str, city: str = "", keyword: str = "", radius_m: int = 3000,
                            sort_by: str = "distance", min_rating: float | None = None,
                            max_cost: float | None = None, limit: int = 6) -> dict:
    """真实周边生活检索；category=restaurant/ktv/cinema/cafe，anchor为地址或高德坐标。价格评分未知时不补造。"""
    return await services.call("maps_local_search", locals())


@mcp.tool()
async def maps_place_detail(poi_id: str) -> dict:
    """按实际 POI ID 查询商家详情；不订座、不订票、不保证实时营业。"""
    return await services.call("maps_place_detail", locals())


@mcp.tool()
async def search_music(keyword: str, limit: int = 5) -> dict:
    """通过 QQ音乐真实快速检索返回歌曲及官方链接；候选数量由平台决定，不自动播放。"""
    return await services.call("search_music", locals())


@mcp.tool()
async def save_itinerary(user_id: str, title: str, idempotency_key: str, details: dict | None = None) -> dict:
    """确认后持久化保存行程；必需稳定幂等键，参数冲突将拒绝写入。"""
    return await services.call("save_itinerary", {**locals(), "details": details or {}})


@mcp.tool()
async def create_reminder(user_id: str, title: str, remind_at: str, idempotency_key: str) -> dict:
    """确认后创建真实站内提醒；时间必须是未来、带时区的 ISO-8601；到期生成站内通知。"""
    return await services.call("create_reminder", locals())


@mcp.tool()
async def list_itineraries(user_id: str) -> dict:
    """读取该用户实际保存的行程清单。"""
    return await services.call("list_itineraries", locals())


@mcp.tool()
async def list_reminders(user_id: str) -> dict:
    """读取该用户实际创建的提醒与 scheduled/delivered/cancelled 状态。"""
    return await services.call("list_reminders", locals())


@mcp.tool()
async def cancel_reminder(user_id: str, reminder_id: str) -> dict:
    """取消该用户尚未触发的提醒；已触发提醒不能撤销通知。"""
    return await services.call("cancel_reminder", locals())


if __name__ == "__main__":
    mcp.run(transport="stdio")
