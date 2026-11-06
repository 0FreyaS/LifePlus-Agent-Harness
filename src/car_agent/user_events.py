"""Only split explicit additional commands; the existing task owns the answer prefix."""
import re

ORDINAL_REPLY = re.compile(r"(?:我选|选择|先选|选|我想要|我要|就要|给我|要)?第?([一二三四五六七八九十]|\d+)(?:个结果|首歌|首|个|项|家|条)?(?:吧|。|！|!)?")


def strict_selection_index(text: str, candidates: list[dict]) -> int | None:
    """Resolve a complete choice expression against one immutable candidate list."""
    text = text.strip().rstrip("。！!")
    text = re.sub(r"^(?:请|帮我)", "", text)
    correction = re.fullmatch(r"不是(.+?)[，,]\s*(?:是|改选|选)(.+)|(.+?)不要[，,]\s*(?:换|改选|选)(.+)", text)
    if correction:
        old, new = (correction[1], correction[2]) if correction[1] else (correction[3], correction[4])
        # Nested corrections or an ungrounded rejected item are not a unique choice.
        if re.search(r"[，,；;]", old + new):
            return None
        previous, selected = strict_selection_index(old, candidates), strict_selection_index(new, candidates)
        return selected if previous is not None and selected is not None and previous != selected else None
    ordinal = re.fullmatch(r"(?:我选|选择|先选|选|我想要|我要|就要|给我|要|就)?第?([一二三四五六七八九十]|\d+)(?:个(?:校区|候选|结果|商家|歌曲|路线)?|首(?:歌|歌曲)?|项|家|条)?吧?", text)
    if ordinal:
        number = ordinal[1]
        index = "一二三四五六七八九十".index(number) + 1 if number in "一二三四五六七八九十" else int(number)
        return index if 1 <= index <= len(candidates) else None
    matched = []
    for index, item in enumerate(candidates, 1):
        name = item.get("name", "")
        aliases = {name, *[alias for alias in item.get("aliases", []) if isinstance(alias, str)]}
        if name.endswith("校区"):
            aliases.add(name[:-2])
        if any(alias and re.fullmatch(r"(?:我选|选择|先选|选|我想要|我要|就要|给我|要|就|去)?"
                + re.escape(alias) + r"(?:那首|这首|那家|这家|那条|这条|那个|这个|的)?吧?", text) for alias in aliases):
            matched.append(index)
    return matched[0] if len(matched) == 1 else None


def selection_index(value: dict, candidates: list[dict]) -> int | None:
    index = value.get("index")
    if type(index) is int and 1 <= index <= len(candidates):
        return index
    text = str(value.get("text", "")).strip()
    if not text:
        return None
    strict = strict_selection_index(text, candidates)
    if strict is not None:
        return strict
    matches = [number for number, item in enumerate(candidates, 1) if item.get("name")
               and (text in item["name"] or item["name"] in text)]
    if len(matches) == 1:
        return matches[0]
    ordinal = ORDINAL_REPLY.fullmatch(text)
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
