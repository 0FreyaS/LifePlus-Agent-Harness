from datetime import date


class DemoServices:
    def query(self, tool_name: str, args: dict) -> dict:
        if tool_name == "maps_local_search":
            return {"items": [{"id": f"local-{index}", "name": f"演示商家{index}", "address": "演示地址",
                               "location": f"120.2{index},31.47", "rating": None, "cost": None, "photos": []}
                              for index in range(1, 4)], "center": "120.26,31.47", "category": args["category"],
                    "source": "固定演示数据", "simulated": True}
        if tool_name == "maps_place_detail":
            return {"item": {"id": args["poi_id"], "name": "演示商家", "address": "演示地址", "location": "120.21,31.47",
                             "rating": None, "cost": None, "phone": None, "opening_hours": None, "photos": []}, "simulated": True}
        if tool_name == "maps_weather":
            city = args["city"]
            return {"city": city, "date": args["forecast_date"], "day_weather": "晴转多云",
                    "low": "16", "high": "24", "source": "固定演示数据", "simulated": True}
        if tool_name == "maps_search":
            keyword = args["keywords"]
            return {"items": [
                {"id": "poi-1", "name": f"{keyword}·湖滨店", "address": "湖滨路 88 号", "location": "120.16,30.25"},
                {"id": "poi-2", "name": f"{keyword}·市民中心店", "address": "解放东路 18 号", "location": "120.21,30.24"},
                {"id": "poi-3", "name": f"{keyword}·城西店", "address": "文二西路 66 号", "location": "120.10,30.28"},
            ], "source": "固定演示数据", "simulated": True}
        if tool_name == "search_music":
            keyword = args["keyword"]
            return {"items": [
                {"id": "song-1", "name": keyword, "singer": "周杰伦", "album": "示例专辑"},
                {"id": "song-2", "name": f"{keyword} (Live)", "singer": "周杰伦", "album": "现场版"},
            ], "source": "固定演示数据", "simulated": True}
        if tool_name == "maps_route":
            return {"origin": args.get("origin", ""), "destination": args.get("destination", ""),
                    "travel_mode": args.get("travel_mode", "driving"), "distance_m": "8200",
                    "duration_s": "1500", "tolls": "0", "steps": ["沿主路向东行驶", "进入目的地道路"],
                    "source": "固定演示数据", "simulated": True}
        return {"error": f"演示服务没有实现工具：{tool_name}"}

