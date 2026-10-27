"""Only split explicit additional commands; the existing task owns the answer prefix."""
import re


def selection_index(value: dict, candidates: list[dict]) -> int | None:
    index = value.get("index")
    if type(index) is int and 1 <= index <= len(candidates):
        return index
    text = str(value.get("text", "")).strip()
    if not text:
        return None
    matches = [number for number, item in enumerate(candidates, 1) if item.get("name")
               and (text in item["name"] or item["name"] in text)]
    if len(matches) == 1:
        return matches[0]
    ordinal = re.fullmatch(r"(?:我选|选择|选)?第?([一二三四五六七八九十]|\d+)(?:个结果|个|项|家|条)?(?:吧|。|！|!)?", text)
    if not ordinal:
        return None
    number = ordinal.group(1)
    index = "一二三四五六七八九十".index(number) + 1 if number in "一二三四五六七八九十" else int(number)
    return index if 1 <= index <= len(candidates) else None


def split_resume_message(text: str) -> tuple[str, str]:
    command = r"(?:帮我|请)?(?:查询|查看|查|搜索|搜|播放|提醒我|创建提醒|保存行程|导航|找|推荐)"
    connector = r"(?:同时|顺便|并且|另外|还要|还有|并|再)"
    pattern = rf"(?:[，,；;]\s*(?:{connector}\s*)?|{connector}\s*)(?={command})"
    pieces = re.split(pattern, text.strip(), maxsplit=1)
    if len(pieces) == 2 and pieces[0].strip() and pieces[1].strip():
        return pieces[0].strip(), pieces[1].strip()
    return text.strip(), ""
