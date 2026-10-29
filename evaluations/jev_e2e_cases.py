"""Small frozen selection for end-to-end Jev ablation, with explicit goal oracles."""
import json
from pathlib import Path

from .completion_dataset import build_cases, goal
from .input_event_cases import COMPLETION_CASES


def build_pilot():
    wanted = {"single_completion-01", "single_completion-09", "single_completion-23",
              "single_completion-26", "single_completion-30", "multi_completion-01", "multi_completion-04",
              "clarified_completion-01", "clarified_completion-29"}
    cases = [case for case in build_cases() if case["id"] in wanted]
    cases += COMPLETION_CASES[:2]
    cases.append({"id": "jev-four-goals", "group": "four_goals", "source": "authored_jev_ab_pilot",
        "query": "查无锡今天的天气，同时搜索周杰伦的歌曲，同时查看我的行程清单，天气查询成功后把完整天气结果保存到行程，标题为无锡天气记录。前三项可以并行，保存只依赖天气结果。",
        "goals": [goal("weather", "maps_weather", {"city": "无锡"}),
                  goal("music", "search_music", {"keyword": "周杰伦"}),
                  goal("planning", "list_itineraries"),
                  goal("planning", "save_itinerary", {"title": "无锡天气记录"}, receipt_from="maps_weather")],
        "user_policy": {}})
    if len(cases) != 12:
        raise ValueError("Pilot source selection changed")
    return cases


if __name__ == "__main__":
    path = Path("evaluations/data/jev-e2e-v1.jsonl")
    if path.exists():
        raise FileExistsError("Do not overwrite a frozen dataset")
    path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in build_pilot()), encoding="utf-8")
    print(json.dumps({"path": str(path), "cases": 12}))
