"""Small authored routing pilot; not production logs or a training corpus."""

QUERIES = {
    "weather": ["无锡今天会下雨吗", "查杭州明天的气温", "南京后天天气怎么样", "上海今晚冷不冷"],
    "navigation": ["杭州东站在哪里", "从杭州东站步行到西湖", "查东南大学无锡校区的地址", "骑行去无锡市图书馆怎么走"],
    "music": ["搜索周杰伦的歌曲", "找一下陶喆的普通朋友", "有没有陈奕迅的十年", "搜一首林俊杰的江南"],
    "planning": ["明天上午九点提醒我带雨伞", "把这次出行保存为行程", "列出我创建的提醒", "查一下已经保存的行程"],
    "wellbeing": ["久坐之后怎样放松肩颈", "睡前有哪些放松的方法", "一般成年人如何安排作息", "日常运动前怎样热身"],
    "local_life": ["找无锡东南大学附近的饭店", "杭州东站附近有没有KTV", "查西湖附近的电影院", "推荐无锡图书馆附近的咖啡店"],
    "chat": ["介绍一下歌手陶喆", "周杰伦是哪一年出道的", "给我解释一下什么是地理坐标", "今天心情不错，陪我聊两句"],
}

CASES = [
    {"id": f"{domain}-{i + 1}", "group": domain, "query": query, "expected_domain": domain,
     "split": "development" if i < 2 else "holdout", "memory_brief": "",
     "source": "人工编写的路由试验，非真实流量"}
    for domain, queries in QUERIES.items() for i, query in enumerate(queries)
]
CASES += [
    {"id": "context-" + domain, "group": "context", "query": query, "expected_domain": domain,
     "split": "holdout", "memory_brief": brief, "source": "人工编写的上下文试验，非真实流量"}
    for domain, query, brief in [
        ("weather", "那明天呢", "用户上一轮询问了无锡今天的天气。"),
        ("navigation", "改成步行怎么走", "用户上一轮规划了杭州东站到西湖的驾车路线。"),
        ("music", "再找一首他的歌", "用户刚才搜索了陶喆的歌曲。"),
        ("planning", "我之前存的有哪些", "用户刚才保存了一条行程，现在想查看已存行程。"),
        ("wellbeing", "睡前做这个合适吗", "用户上一轮讨论一般肩颈放松运动，不涉及诊断或治疗。"),
        ("local_life", "附近还有咖啡店吗", "当前参考地点是无锡市图书馆，用户先前寻找附近饭店。"),
        ("chat", "介绍一下他", "用户刚才讨论的歌手是陶喆，现在想了解歌手生平。"),
    ]
]
