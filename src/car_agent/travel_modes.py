"""Current-query transport constraints; never infer them from old conversations."""
from __future__ import annotations

import re

TRAVEL_MODES = {"driving": "驾车", "walking": "步行", "bicycling": "骑行"}
_PATTERNS = {
    "driving": r"驾车|开车|自驾|\bdriving\b",
    "walking": r"步行(?!街|道)|走路|徒步|\bwalking\b",
    "bicycling": r"骑自行车|骑单车|骑车|骑行|\b(?:bicycling|cycling)\b",
}
_NEGATED = re.compile(r"(?:不要|不想|不用|不打算|不考虑|不选择|不选|不是|不能|别|不)(?:再|选择|使用|采用|考虑)?\s*$")


def explicit_travel_mode(query: str) -> str | None:
    mentions = []
    for mode, pattern in _PATTERNS.items():
        for match in re.finditer(pattern, query, re.IGNORECASE):
            prefix = re.split(r"[，,。！？；;\n]", query[:match.start()])[-1][-12:]
            if not _NEGATED.search(prefix):
                mentions.append((match.start(), match.end(), mode))
    mentions.sort()
    modes = {mode for _, _, mode in mentions}
    if len(modes) == 1:
        return mentions[-1][2]
    if len(modes) > 1:
        # Resolve explicit corrections, but do not turn a comparison into a preference.
        previous, last = mentions[-2:]
        if re.search(r"改成|改为|换成|换为|而是|现在(?:想|要|改|选择|用)", query[previous[1]:last[0]]):
            return last[2]
    return None


def mode_choices(default: str) -> list[dict]:
    ordered = [default, *(mode for mode in TRAVEL_MODES if mode != default)]
    return [{"travel_mode": mode, "label": TRAVEL_MODES[mode], "is_default": mode == default} for mode in ordered if mode in TRAVEL_MODES]
