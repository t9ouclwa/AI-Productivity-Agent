import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path


_VAULT_DIR_RAW = os.getenv("VAULT_DIR", "").strip()
VAULT_DIR: Path | None = Path(_VAULT_DIR_RAW).expanduser() if _VAULT_DIR_RAW else None
BASE = "语言学习与记录"
COMMON_LINKS = [
    "[[DOCTOR总览]]",
    "[[语言学习与记录/待办|待办]]",
]


def _root() -> Path:
    if VAULT_DIR is None:
        raise RuntimeError("VAULT_DIR is not set")
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    return VAULT_DIR


def _append(path: Path, title: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {title}\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n\n")
    ensure_links(path, links_for(path))


def links_for(path: Path) -> list[str]:
    root = _root()
    try:
        rel = path.relative_to(root)
    except ValueError:
        return COMMON_LINKS

    links = list(COMMON_LINKS)
    rel_text = str(rel)
    if rel_text.startswith(f"{BASE}/日常生活/"):
        links.append("[[语言学习与记录/00-Inbox|00-Inbox]]")
    if rel_text.startswith("研究进度/") or rel_text.startswith("实验记录/"):
        links.append("[[研究进度/研究进度总览|研究进度总览]]")
    if rel_text == f"{BASE}/待办.md":
        today = date.today().isoformat()
        links.extend(
            [
                f"[[语言学习与记录/日常生活/{today}|今天的日常]]",
                "[[研究进度/研究进度总览|研究进度总览]]",
            ]
        )

    deduped: list[str] = []
    for link in links:
        if link not in deduped:
            deduped.append(link)
    return deduped


def ensure_links(path: Path, links: list[str] | None = None) -> None:
    links = links or COMMON_LINKS
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    cleaned = _strip_obsidian_link_blocks(text)
    existing_links = [
        link for link in re.findall(r"\[\[[^\]]+\]\]", cleaned)
        if _is_real_obsidian_link(link)
    ]
    final_links: list[str] = []
    for link in [*existing_links, *links]:
        if link not in final_links:
            final_links.append(link)
    if not final_links:
        return
    block = "\n\n## Obsidian 关联\n\n" + "\n".join(f"- {link}" for link in final_links)
    new_text = cleaned.rstrip() + block + "\n"
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")


def _is_real_obsidian_link(link: str) -> bool:
    inner = link.strip()[2:-2].strip()
    if inner.startswith(("ZH:", "ZH：")):
        return False
    if not inner:
        return False
    return True


def _strip_obsidian_link_blocks(text: str) -> str:
    lines = text.splitlines()
    cleaned: list[str] = []
    in_link_block = False
    for line in lines:
        if line.strip() == "## Obsidian 关联":
            in_link_block = True
            continue
        if in_link_block:
            if not line.strip():
                continue
            if re.match(r"^\s*- \[\[[^\]]+\]\]\s*$", line):
                continue
            in_link_block = False
        if re.match(r"^\s*- \[\[[^\]]+\]\]\s*$", line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).rstrip() + "\n"


def ensure_vault_links() -> list[Path]:
    root = _root()
    changed: list[Path] = []
    for path in root.rglob("*.md"):
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        ensure_links(path, links_for(path))
        after = path.read_text(encoding="utf-8") if path.exists() else ""
        if after != before:
            changed.append(path)
    return changed


def _clean_todo_item(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" -，,。")
    return text


def append_todos(items: list[str], due: str | None = None, source: str = "") -> Path:
    root = _root()
    path = root / BASE / "待办.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    for item in items:
        clean = _clean_todo_item(item)
        if not clean:
            continue
        line = f"- [ ] {clean}"
        if due:
            line += f"  📅 {due}"
        if source and source.strip() != clean:
            line += f"  #来源/{stamp[:10]}"
        line += f" (记于 {stamp})"
        lines.append(line)
    if lines:
        _append(path, "待办", "\n".join(lines))
    return path


def append_note(category: str, raw: str, reply: str = "", due: str | None = None, todos: list[str] | None = None) -> Path:
    today = date.today().isoformat()
    root = _root()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if category == "待办" or due or todos:
        items = todos or split_todo_text(raw)
        path = append_todos(items, due=due, source=raw)
    elif category == "实验":
        path = root / "实验记录" / f"{today}.md"
        _append(path, f"实验记录 {today}", f"## {stamp}\n\n{raw}")
    elif category == "学习":
        path = root / BASE / "学习日志" / f"{today}.md"
        _append(path, f"学习日志 {today}", f"## {stamp}\n\n{raw}")
    else:
        path = root / BASE / "00-Inbox.md"
        if not _is_trivial_chat(raw):
            _append(path, "Inbox", f"- {stamp} {raw}")
    return path


def split_todo_text(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[；;。\n]+|(?:，|,)\s*(?=(?:然后|再|还要|还有|接着|顺便|另外|以及|并且|做|去|取|看|写|整理|准备|提醒|学习|复习|洗|睡|吃))", text)
    cleaned = [_clean_todo_item(part) for part in parts]
    return [part for part in cleaned if part]


def append_word(word: str, card: str) -> Path:
    path = _root() / BASE / "生词本" / "生词.md"
    _append(path, "生词本", card)
    return path


def append_life_event(text: str, source: str = "chat") -> Path:
    today = date.today().isoformat()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    path = _root() / BASE / "日常生活" / f"{today}.md"
    cleaned = _strip_translation_markers(text)
    if _is_trivial_life_event(cleaned, source):
        return path
    _append(path, f"日常生活 {today}", f"- {stamp} [{source}] {cleaned}")
    return path


def _strip_translation_markers(text: str) -> str:
    return re.sub(r"\s*\[\[ZH[：:][^\]]+\]\]\s*", " ", text or "").strip()


def _normalize_chat_text(text: str) -> str:
    text = _strip_translation_markers(text)
    text = re.sub(r"\s+", "", text).strip("。！？!?.,，、…")
    return text.lower()


def _is_trivial_chat(text: str) -> bool:
    compact = _normalize_chat_text(text)
    if not compact:
        return True
    trivial = {
        "嗯", "好", "好吧", "好的", "可以", "停", "hi", "hello", "doctor",
        "你呢", "人呢", "好耶", "对不起", "哈哈", "笑死", "不知道", "。。。", "....",
    }
    return compact in trivial


def _is_trivial_life_event(text: str, source: str) -> bool:
    compact = _normalize_chat_text(text)
    if _is_trivial_chat(text):
        return True
    if source == "checkin" and "[spark主动消息]" in text:
        return True
    if source == "telegram" and len(compact) <= 2:
        useful_short = {"到家", "睡醒", "起床", "取了", "晾了", "下课", "洗澡", "睡觉"}
        return compact not in useful_short
    return False


def recent_life(max_chars: int = 8000) -> str:
    folder = _root() / BASE / "日常生活"
    if not folder.exists():
        return ""
    chunks: list[str] = []
    total = 0
    files = sorted(folder.glob("*.md"), key=lambda p: p.name, reverse=True)
    for path in files:
        text = path.read_text(encoding="utf-8")
        block = f"\n# {path.name}\n{text}"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 0:
                chunks.append(block[:remaining])
            break
        chunks.append(block)
        total += len(block)
    return "\n".join(chunks)


def _parse_life_line(line: str) -> tuple[datetime | None, str, str] | None:
    match = re.match(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \[([^\]]+)\] (.*)$", line.strip())
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M")
    except ValueError:
        stamp = None
    return stamp, match.group(2), match.group(3).strip()


def recent_life_events(days: int = 3) -> list[dict]:
    folder = _root() / BASE / "日常生活"
    if not folder.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    events: list[tuple[datetime, str, str]] = []
    for path in sorted(folder.glob("*.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_life_line(line)
            if not parsed:
                continue
            stamp, source, text = parsed
            if stamp and stamp >= cutoff:
                events.append((stamp, source, text))
    events.sort(key=lambda item: item[0])
    return [{"time": stamp.strftime("%Y-%m-%d %H:%M"), "source": source, "text": text} for stamp, source, text in events]


def rough_life_summary(days: int = 3, max_groups: int = 12) -> str:
    events = recent_life_events(days=days)
    if not events:
        return ""
    groups: list[list[dict]] = []
    current: list[dict] = []
    last_time: datetime | None = None
    for event in events:
        stamp = datetime.strptime(event["time"], "%Y-%m-%d %H:%M")
        text = event["text"]
        starts_new = False
        if last_time and stamp - last_time > timedelta(minutes=25):
            starts_new = True
        if current and re.search(r"(睡醒|早上好|开完|做完|结束|回家|出门|到了|准备|现在|今天|明天)", text):
            starts_new = True
        if starts_new:
            groups.append(current)
            current = []
        current.append(event)
        last_time = stamp
    if current:
        groups.append(current)
    groups = groups[-max_groups:]

    lines = ["最近生活事件摘要："]
    for group in groups:
        start = group[0]["time"]
        end = group[-1]["time"]
        texts = [item["text"] for item in group]
        merged = " / ".join(texts)
        if len(merged) > 160:
            merged = merged[:157] + "..."
        time_label = start if start == end else f"{start} - {end[-5:]}"
        lines.append(f"- {time_label}：{merged}")
    return "\n".join(lines)


def collect_markdown(max_chars: int = 60000) -> str:
    root = _root()
    files = sorted(root.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    chunks: list[str] = []
    total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        block = f"\n\n# FILE: {path.relative_to(root)}\n{text}"
        if total + len(block) > max_chars:
            break
        chunks.append(block)
        total += len(block)
    return "".join(chunks)


def open_todos() -> list[str]:
    path = _root() / BASE / "待办.md"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- [ ]")]


def complete_todos(keyword: str) -> int:
    path = _root() / BASE / "待办.md"
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    count = 0
    lines = []
    for line in text.splitlines():
        if line.startswith("- [ ]") and (not keyword or keyword in line):
            line = re.sub(r"^- \[ \]", "- [x]", line, count=1)
            count += 1
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count


def auto_complete_from_text(text: str) -> int:
    clean = (text or "").strip()
    if not clean:
        return 0
    done_markers = ["做完", "完成", "弄完", "写完", "整理完", "看完", "取了", "拿了", "洗好了", "吃完", "交了", "提交", "終わった", "できた"]
    if not any(marker in clean for marker in done_markers):
        return 0

    candidates = []
    patterns = [
        r"(.{1,24}?)(?:做完了|做完|完成了|完成|弄完了|弄完|写完了|写完|整理完了|整理完|看完了|看完)",
        r"(?:做完了|做完|完成了|完成|弄完了|弄完|写完了|写完|整理完了|整理完|看完了|看完)(.{0,24})",
        r"(取钱|钱|洗澡|PPT|ppt|轮讲|稿子|研究|标注|邮件|Gmail|gmail|麦片|课|会议|面谈)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, clean, re.I):
            value = " ".join(group for group in match.groups() if group).strip(" ，,。")
            if value:
                candidates.append(value)

    if not candidates:
        candidates = split_todo_text(clean)

    total = 0
    seen = set()
    for candidate in candidates:
        keyword = candidate.strip()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        total += complete_todos(keyword)
    return total
