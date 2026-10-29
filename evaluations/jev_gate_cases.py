"""Frozen authored cardinality pilot; not production traffic or independent users."""

SINGLE = [
    ("查无锡天气", "weather"),
    ("查无锡天气，谢谢", "weather"),
    ("明天呢", "weather"),
    ("从杭州东站步行到西湖", "navigation"),
    ("从120.272791,31.474621步行到东南大学无锡校区", "navigation"),
    ("先找到东南大学然后导航过去", "navigation"),
    ("搜索周杰伦的歌曲", "music"),
    ("介绍周杰伦", "chat"),
    ("搜索歌曲《你和我》", "music"),
    ("帮我找无锡市梁溪区和平路的电影院", "local_life"),
    ("找个适合聊天、有停车位的无锡咖啡店", "local_life"),
    ("查一查明天下午无锡下不下雨以及气温", "weather"),
    ("保存名为音乐和旅行的行程", "planning"),
    ("提醒我明早八点带雨伞和钥匙", "planning"),
    ("久坐后腰不舒服有什么日常活动建议", "wellbeing"),
]
MULTI = [
    "查无锡天气，搜索周杰伦歌曲", "无锡天气和南京天气都查一下",
    "查无锡天气然后保存查询结果到行程", "从杭州东站步行到西湖并且搜索周杰伦歌曲",
    "查天气同时帮我查提醒列表", "搜索歌曲晴天再介绍一下演唱者周杰伦",
    "无锡天气周杰伦歌曲都要", "我要无锡明日天气外加周杰伦的歌",
    "附近电影院找一家再帮我查无锡天气", "先查天气成功后再搜索周杰伦歌曲",
    "列出我的行程另外看看明天无锡下雨吗", "找无锡咖啡店再搜索陶喆的歌",
    "介绍陶喆并搜索他的歌曲", "帮我查无锡天气、步行去西湖、搜索晴天、列出提醒",
    "查南京天气并查杭州天气，两个城市都要",
]
CASES = [{"id": f"count-single-{i:02}", "query": query, "expected_label": "single", "expected_domain": domain,
          "group": "single", "context": "上轮用户明确查询无锡天气。" if query == "明天呢" else ""}
         for i, (query, domain) in enumerate(SINGLE, 1)] + [
         {"id": f"count-multi-{i:02}", "query": query, "expected_label": "multi", "group": "multi", "context": ""}
         for i, query in enumerate(MULTI, 1)]
