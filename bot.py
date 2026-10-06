import asyncio
import html
import json
import logging
import os
import re
import shutil
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import llm
import gmail_client
import prompts
import vault


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
VOICE_MODEL = os.getenv("VOICE_MODEL", llm.VOICE_MODEL)
CHAT_ID_FILE = Path(".chat_id")
GMAIL_STATE_FILE = Path(".gmail_state.json")
CHECKIN_STATE_FILE = Path(".checkin_state.json")
SPARK_STATE_FILE = Path(".spark_state.json")
REMINDERS_STATE_FILE = Path(".reminders_state.json")
CHAT_HISTORY_FILE = Path(".chat_history.json")
RELATIONSHIP_STATE_FILE = Path(".relationship_state.json")
MEDIA_DIR = Path("media_inbox")
GMAIL_CHECK_INTERVAL_SEC = int(os.getenv("GMAIL_CHECK_INTERVAL_SEC", "600"))
CHECKIN_INTERVAL_SEC = int(os.getenv("CHECKIN_INTERVAL_SEC", str(2 * 60 * 60)))
IDLE_CHECKIN_MIN_IDLE_SEC = int(os.getenv("IDLE_CHECKIN_MIN_IDLE_SEC", str(4 * 60 * 60)))
IDLE_CHECKIN_COOLDOWN_SEC = int(os.getenv("IDLE_CHECKIN_COOLDOWN_SEC", str(4 * 60 * 60)))
SPARK_INTERVAL_SEC = int(os.getenv("SPARK_INTERVAL_SEC", str(30 * 60)))
SPARK_MIN_IDLE_SEC = int(os.getenv("SPARK_MIN_IDLE_SEC", str(30 * 60)))
SPARK_COOLDOWN_SEC = int(os.getenv("SPARK_COOLDOWN_SEC", str(90 * 60)))
CHAT_HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "20"))
DOCTOR_CONFIG_DIR = Path(os.path.expanduser("~/.config/doctor"))
MEMORY_FILE = DOCTOR_CONFIG_DIR / "memory.md"
USER_PROFILE_FILE = DOCTOR_CONFIG_DIR / "user_profile.md"
CHAT_INPUT_BUFFERS: dict[str, list[str]] = {}
CHAT_LAST_UPDATE: dict[str, Update] = {}
CHAT_LAST_CONTEXT: dict[str, ContextTypes.DEFAULT_TYPE] = {}
CHAT_WORKERS: dict[str, asyncio.Task] = {}
JOB_KWARGS = {"misfire_grace_time": int(os.getenv("JOB_MISFIRE_GRACE_SEC", "3600"))}
QUIET_KEYWORDS = re.compile(
    r"(下课|上课|授業|講義|课|会议|会議|开会|打工|考试|試験|発表|汇报|報告|忙|睡|休息|"
    r"之后再|以后再|以後|后再|後で|再联系|再聯絡|連絡して|来找我|再来|找我|叫我|喊我|"
    r"别问|不要问|不要打扰|静音|勿扰)"
)


async def _reply(update: Update, text: str) -> None:
    if update.message:
        await update.message.chat.send_action(ChatAction.TYPING)
        await asyncio.sleep(0.4)
        bubbles = _split_bubbles(text)
        first = True
        for bubble in bubbles:
            html_bubble = _telegram_html(bubble)
            if first:
                await update.message.reply_text(html_bubble, parse_mode="HTML")
                first = False
            else:
                await asyncio.sleep(0.35)
                await update.message.chat.send_action(ChatAction.TYPING)
                await asyncio.sleep(0.25)
                await update.message.chat.send_message(html_bubble, parse_mode="HTML")


def _telegram_html(text: str) -> str:
    """Escape Telegram HTML while preserving [[ZH:...]] as tap-to-reveal spoilers."""
    raw = text or ""
    pattern = re.compile(r"\[\[ZH[:：](.*?)\]\]", re.S)
    parts: list[str] = []
    pos = 0
    for match in pattern.finditer(raw):
        parts.append(html.escape(raw[pos : match.start()]))
        translation = match.group(1).strip()
        if translation:
            parts.append(f'<tg-spoiler>{html.escape(translation)}</tg-spoiler>')
        pos = match.end()
    parts.append(html.escape(raw[pos:]))
    return "".join(parts) or "。"


def _protect_translation_blocks(text: str) -> tuple[str, str]:
    placeholder = "\uE000"
    pattern = re.compile(r"\[\[ZH[:：].*?\]\]", re.S)
    protected = pattern.sub(lambda match: match.group(0).replace("\n", placeholder), text)
    return protected, placeholder


def _split_bubbles(text: str, limit: int | None = None, max_bubbles: int | None = None) -> list[str]:
    limit = limit or int(os.getenv("REPLY_BUBBLE_CHARS", "3400"))
    max_bubbles = max_bubbles or int(os.getenv("MAX_REPLY_BUBBLES", "20"))
    clean = (text or "").strip()
    if not clean:
        return ["。"]

    marker = "<<BUBBLE>>"
    protected, translation_newline = _protect_translation_blocks(clean)
    has_translation_block = bool(re.search(r"\[\[ZH[:：]", clean))
    if marker in protected:
        paragraphs = [part.strip() for part in protected.split(marker) if part.strip()]
    else:
        paragraphs = [part.strip() for part in protected.split("\n\n") if part.strip()]
        if len(paragraphs) <= 1 and not has_translation_block:
            paragraphs = [part.strip() for part in protected.splitlines() if part.strip()] or [protected]

    bubbles: list[str] = []
    for paragraph in paragraphs:
        paragraph = paragraph.replace(translation_newline, "\n")
        if len(paragraph) <= limit:
            bubbles.append(paragraph)
            continue
        for start in range(0, len(paragraph), limit):
            bubbles.append(paragraph[start : start + limit])

    if len(bubbles) > max_bubbles:
        bubbles = bubbles[:max_bubbles]
        bubbles.append("后面还有内容，但我先停在这里。要继续就说“继续”。")
    return [bubble[:3900] for bubble in bubbles if bubble.strip()]


def _load_chat_history() -> dict:
    if not CHAT_HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(CHAT_HISTORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_chat_history(data: dict) -> None:
    CHAT_HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _remember(chat_id: int | str | None, role: str, text: str) -> None:
    if chat_id is None or not text:
        return
    data = _load_chat_history()
    key = str(chat_id)
    turns = data.get(key, [])
    turns.append({"role": role, "text": text[:1000]})
    data[key] = turns[-CHAT_HISTORY_LIMIT:]
    _save_chat_history(data)


def _context_for(chat_id: int | str | None) -> str:
    if chat_id is None:
        return ""
    turns = _load_chat_history().get(str(chat_id), [])
    lines = []
    for turn in turns[-CHAT_HISTORY_LIMIT:]:
        role = "用户" if turn.get("role") == "user" else "DOCTOR"
        lines.append(f"{role}: {turn.get('text', '')}")
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_relationship_state() -> dict:
    if not RELATIONSHIP_STATE_FILE.exists():
        return {}
    try:
        return json.loads(RELATIONSHIP_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_relationship_state(data: dict) -> None:
    RELATIONSHIP_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _touch_relationship(chat_id: int | str | None) -> dict:
    if chat_id is None:
        return {}
    data = _load_relationship_state()
    key = str(chat_id)
    state = data.get(key, {})
    now = _now_iso()
    state.setdefault("first_seen", now)
    state["last_seen"] = now
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    data[key] = state
    _save_relationship_state(data)
    return state


def _relationship_text(chat_id: int | str | None) -> str:
    if chat_id is None:
        return "还没有足够的相处记录。"
    state = _load_relationship_state().get(str(chat_id), {})
    turns = int(state.get("turn_count", 0))
    first_seen = state.get("first_seen")
    days = 0
    if first_seen:
        try:
            first = datetime.fromisoformat(first_seen)
            days = max(0, (datetime.now(timezone.utc) - first).days)
        except ValueError:
            days = 0
    if turns >= 300 or days >= 30:
        level = "默契期：可以更主动地预测用户节奏，但仍要说明不确定性。"
    elif turns >= 120 or days >= 14:
        level = "熟悉期：已经能参考用户习惯，语气可以更少解释、更像长期搭档。"
    elif turns >= 30 or days >= 3:
        level = "升温期：开始形成相处习惯，可以轻微预测用户下一步。"
    else:
        level = "初识期：保持克制、可靠，少做过度亲密推断。"
    return f"相处天数约 {days} 天；累计对话轮次 {turns}；熟悉程度：{level}"


def _last_user_seen(chat_id: int | str | None) -> datetime | None:
    if chat_id is None:
        return None
    state = _load_relationship_state().get(str(chat_id), {})
    return _parse_iso_datetime(state.get("last_seen"))


def _format_duration_ja(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}時間{mins}分"
    if hours:
        return f"{hours}時間"
    return f"{mins}分"


def _load_spark_state() -> dict:
    if not SPARK_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SPARK_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_spark_state(state: dict) -> None:
    SPARK_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_proactive(kind: str, text: str = "") -> None:
    state = _load_spark_state()
    state["last_sent_at"] = _now_iso()
    state["last_kind"] = kind
    if text:
        state["last_text"] = text[:160]
    _save_spark_state(state)


def _last_proactive_at() -> datetime | None:
    return _parse_iso_datetime(_load_spark_state().get("last_sent_at"))


def _life_context() -> str:
    try:
        return vault.recent_life()
    except Exception:
        logging.exception("read life context failed")
        return ""


def _append_memory(text: str) -> None:
    DOCTOR_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text("# DOCTOR 可塑层\n\n", encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- {stamp} {text.strip()}\n")


def _read_memory() -> str:
    if not MEMORY_FILE.exists():
        return "还没有可塑层记忆。"
    return MEMORY_FILE.read_text(encoding="utf-8").strip() or "还没有可塑层记忆。"


def _append_user_profile(text: str) -> None:
    DOCTOR_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not USER_PROFILE_FILE.exists():
        USER_PROFILE_FILE.write_text("# 用户基础档案\n\n## 补充记录\n", encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with USER_PROFILE_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- {stamp} {text.strip()}\n")


def _read_user_profile() -> str:
    if not USER_PROFILE_FILE.exists():
        return "还没有用户基础档案。"
    return USER_PROFILE_FILE.read_text(encoding="utf-8").strip() or "还没有用户基础档案。"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        CHAT_ID_FILE.write_text(str(chat_id), encoding="utf-8")
    await _reply(
        update,
        f"DOCTOR 已启动。\nchat_id: {chat_id}\n\n直接发消息我会帮你归档；/ask 查笔记；/word 存生词；/done 看待办。",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        "/start 初始化\n/ask 问题 - 查笔记\n/word 单词 - 存生词\n/done [关键词] - 查看或完成待办\n/remind 时间 内容 - 设置一次性提醒\n/remind 每天20:00 内容 - 设置每日提醒\n/gmail - 查看未读邮件摘要\n/gmail_on - 开启邮件主动提醒\n/gmail_off - 关闭邮件主动提醒\n/checkin_on - 开启主动问候和记忆驱动 spark\n/checkin_off - 关闭主动问候和 spark\n/profile - 查看用户基础档案\n/profile_add 内容 - 补充用户基础档案\n/remember 内容 - 写入可塑层\n/memory - 查看可塑层\n/life - 查看近期日常记录\n/familiarity - 查看熟悉程度\n/reset_context - 清空最近对话上下文\n\n直接发消息：自动归档并回复。",
    )


async def reset_context_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    data = _load_chat_history()
    if chat_id is not None:
        data.pop(str(chat_id), None)
        _save_chat_history(data)
    await _reply(update, "最近对话上下文已清空。")


async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args).strip()
    if not question:
        await _reply(update, "用法：/ask 你的问题")
        return
    notes = vault.collect_markdown()
    if not notes.strip():
        await _reply(update, "现在 vault 里还没有笔记。")
        return
    try:
        chat_id = update.effective_chat.id if update.effective_chat else None
        answer = await llm.ask(prompts.answer_prompt(question, notes, _relationship_text(chat_id), _life_context()), model=VOICE_MODEL)
    except Exception as e:
        logging.exception("ask failed")
        answer = f"查笔记时出错了：{e}"
    await _reply(update, answer)


async def word_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    word = " ".join(context.args).strip()
    if not word:
        await _reply(update, "用法：/word 单词")
        return
    try:
        card = await llm.ask(prompts.word_prompt(word), model=llm.MODEL)
    except Exception as e:
        logging.exception("word failed")
        card = f"## {word}\n\n生成生词卡失败：{e}"
    path = vault.append_word(word, card)
    await _reply(update, f"记进生词本了：{path.name}")


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyword = " ".join(context.args).strip()
    if keyword:
        count = vault.complete_todos(keyword)
        await _reply(update, f"已完成 {count} 条待办。")
        return
    todos = vault.open_todos()
    await _reply(update, "当前没有未完成待办。" if not todos else "\n".join(todos[:20]))


async def _gmail_summary(messages: list[dict]) -> str:
    formatted = gmail_client.format_messages(messages)
    if not messages:
        return formatted
    try:
        return await llm.ask(prompts.gmail_summary_prompt(formatted, life_context=_life_context()), model=VOICE_MODEL)
    except Exception:
        logging.exception("gmail summary failed")
        return formatted


async def gmail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        messages = await asyncio.to_thread(gmail_client.list_unread, 8)
        summary = await _gmail_summary(messages)
    except Exception as e:
        logging.exception("gmail failed")
        summary = f"Gmail 读取失败：{e}\n\n如果是第一次使用，先在终端运行：\ncd ~/doctor-bot\n./.venv/bin/python -m gmail_client"
    await _reply(update, summary)


async def gmail_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = gmail_client.load_state(GMAIL_STATE_FILE)
    state["enabled"] = True
    gmail_client.save_state(GMAIL_STATE_FILE, state)
    await _reply(update, "已开启 Gmail 主动提醒。我会定时检查未读邮件，只提醒新邮件。")


async def gmail_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = gmail_client.load_state(GMAIL_STATE_FILE)
    state["enabled"] = False
    gmail_client.save_state(GMAIL_STATE_FILE, state)
    await _reply(update, "已关闭 Gmail 主动提醒。")


async def gmail_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = gmail_client.load_state(GMAIL_STATE_FILE)
    if not state.get("enabled"):
        return
    chat_id = os.getenv("CHAT_ID") or (CHAT_ID_FILE.read_text(encoding="utf-8").strip() if CHAT_ID_FILE.exists() else "")
    if not chat_id:
        return
    try:
        messages = await asyncio.to_thread(gmail_client.list_unread, 8)
    except Exception:
        logging.exception("gmail job failed")
        return

    seen = set(state.get("seen_ids", []))
    fresh = [msg for msg in messages if msg.get("id") not in seen]
    if not fresh:
        return

    summary = await _gmail_summary(fresh[:5])
    message = f"你有新的未读邮件：\n\n{summary}"[:3900]
    await context.bot.send_message(chat_id=chat_id, text=_telegram_html(message), parse_mode="HTML")
    state["seen_ids"] = list((seen | {msg["id"] for msg in messages}) - {""})[-200:]
    gmail_client.save_state(GMAIL_STATE_FILE, state)


def _load_checkin_state() -> dict:
    if not CHECKIN_STATE_FILE.exists():
        return {"enabled": False}
    try:
        import json

        return json.loads(CHECKIN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False}


def _save_checkin_state(state: dict) -> None:
    import json

    CHECKIN_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_checkin_state(**changes) -> dict:
    state = _load_checkin_state()
    state.update(changes)
    _save_checkin_state(state)
    return state


def _load_reminders_state() -> list[dict]:
    if not REMINDERS_STATE_FILE.exists():
        return []
    try:
        data = json.loads(REMINDERS_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_reminders_state(reminders: list[dict]) -> None:
    REMINDERS_STATE_FILE.write_text(json.dumps(reminders, ensure_ascii=False, indent=2), encoding="utf-8")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_date_hint(clean: str, now: datetime) -> tuple[date | None, bool]:
    today = now.date()
    relative_days = {
        "今天": 0,
        "今日": 0,
        "明天": 1,
        "明日": 1,
        "あした": 1,
        "明後天": 2,
        "后天": 2,
        "後天": 2,
        "大后天": 3,
        "大後天": 3,
    }
    for word, days in relative_days.items():
        if word in clean:
            return (now + timedelta(days=days)).date(), True

    numeric = re.search(r"(?:(20\d{2})\s*[年/-])?\s*(\d{1,2})\s*(?:月|/|-)\s*(\d{1,2})\s*(?:日|号)?", clean)
    if numeric:
        year = int(numeric.group(1) or now.year)
        month = int(numeric.group(2))
        day = int(numeric.group(3))
        try:
            target = now.replace(year=year, month=month, day=day).date()
        except ValueError:
            return None, True
        if numeric.group(1) is None and target < today:
            try:
                target = target.replace(year=target.year + 1)
            except ValueError:
                return None, True
        return target, True

    weekday_match = re.search(r"(下周|下週|下星期|下礼拜|来周|来週)?\s*(?:周|週|星期|礼拜)([一二三四五六日天])", clean)
    if weekday_match:
        weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        target_weekday = weekday_map[weekday_match.group(2)]
        days_ahead = (target_weekday - today.weekday()) % 7
        has_next_prefix = bool(weekday_match.group(1))
        if has_next_prefix:
            days_ahead = days_ahead + 7 if days_ahead == 0 else days_ahead
        elif days_ahead == 0:
            days_ahead = 7
        return (now + timedelta(days=days_ahead)).date(), True

    return None, False


def _parse_clock(clean: str, now: datetime) -> tuple[datetime | None, re.Match[str] | None]:
    absolute = re.search(r"(上午|早上|中午|下午|晚上|今晚|今天)?\s*(\d{1,2})\s*(?::|：|点)\s*(半|\d{0,2})", clean)
    if not absolute:
        return None, None

    target_date, has_explicit_date = _parse_date_hint(clean, now)
    if has_explicit_date and target_date is None:
        return None, absolute

    period = absolute.group(1) or ""
    hour = int(absolute.group(2))
    minute_raw = absolute.group(3) or "0"
    minute = 30 if minute_raw == "半" else int(minute_raw or "0")
    if period == "中午" and hour < 11:
        hour += 12
    if period in {"下午", "晚上", "今晚"} and hour < 12:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, absolute

    date_for_run = target_date or now.date()
    run_at = now.replace(
        year=date_for_run.year,
        month=date_for_run.month,
        day=date_for_run.day,
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if not has_explicit_date and run_at <= now:
        run_at += timedelta(days=1)
    return run_at, absolute


def _parse_quiet_until(text: str) -> datetime | None:
    clean = text.strip()
    if not QUIET_KEYWORDS.search(clean):
        return None
    if re.search(r"(每周|每週|毎週|每星期|每礼拜|每禮拜)", clean) and not re.search(
        r"(到|直到|まで).*(再|来找我|找我|联系|聯絡|連絡)|(?:再|来找我|找我|联系|聯絡|連絡).*(到|直到|まで)",
        clean,
    ):
        return None
    if re.search(r"(每天|每日|每晚|每天晚上|毎日|毎晩)", clean) and not re.search(
        r"(到|直到|まで).*(再|来找我|找我|联系|聯絡|連絡)|(?:再|来找我|找我|联系|聯絡|連絡).*(到|直到|まで)",
        clean,
    ):
        return None
    now = _local_now()
    relative = re.search(
        r"(?:过|等我)?\s*(\d+|[零一二两三四五六七八九十]+|半)\s*"
        r"(分钟|分|小时|个小时|h|m)\s*"
        r"(?:后|後|以后|以後|之后|之後)?\s*"
        r"(?:再)?\s*(?:来找我|来找|找我|来|联系|聯絡|連絡|叫我|喊我)?",
        clean,
        re.I,
    )
    if relative:
        matched = relative.group(0)
        has_followup_word = re.search(r"后|後|以后|以後|之后|之後|再|来|找我|联系|聯絡|連絡|叫我|喊我|等我|过|忙", matched + clean)
        if not has_followup_word:
            return None
        try:
            amount = _parse_chinese_amount(relative.group(1))
        except ValueError:
            return None
        unit = relative.group(2)
        return now + (timedelta(hours=amount) if unit in {"小时", "个小时", "h"} else timedelta(minutes=amount))

    run_at, _ = _parse_clock(clean, now)
    if not run_at or run_at <= now:
        return None
    return run_at


def _quiet_followup_job_name() -> str:
    return "quiet-followup"


def _clear_jobs_by_name(job_queue, name: str) -> None:
    try:
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    except Exception:
        logging.exception("clear jobs failed: %s", name)


def _schedule_quiet_followup(job_queue, quiet_until: datetime, reason: str) -> None:
    state = _load_checkin_state()
    if not state.get("enabled"):
        return
    chat_id = os.getenv("CHAT_ID") or (CHAT_ID_FILE.read_text(encoding="utf-8").strip() if CHAT_ID_FILE.exists() else "")
    if not chat_id:
        return
    delay = max(1, (quiet_until - _local_now()).total_seconds())
    _clear_jobs_by_name(job_queue, _quiet_followup_job_name())
    job_queue.run_once(
        quiet_followup_job,
        when=delay,
        data={"chat_id": str(chat_id), "quiet_until": quiet_until.isoformat(), "reason": reason},
        name=_quiet_followup_job_name(),
        job_kwargs=JOB_KWARGS,
    )


def _maybe_set_quiet_until(text: str, job_queue=None) -> datetime | None:
    quiet_until = _parse_quiet_until(text)
    if not quiet_until:
        return None
    state = _load_checkin_state()
    current_raw = state.get("quiet_until")
    try:
        current = datetime.fromisoformat(current_raw) if current_raw else None
    except Exception:
        current = None
    if current is None or quiet_until > current or current <= _local_now():
        _update_checkin_state(quiet_until=quiet_until.isoformat(), quiet_reason=text[:80])
        if job_queue is not None:
            _schedule_quiet_followup(job_queue, quiet_until, text[:80])
    return quiet_until


def _checkin_quiet_until() -> datetime | None:
    state = _load_checkin_state()
    raw = state.get("quiet_until")
    if not raw:
        return None
    try:
        quiet_until = datetime.fromisoformat(raw)
    except Exception:
        _update_checkin_state(quiet_until=None, quiet_reason=None)
        return None
    if quiet_until <= _local_now():
        _update_checkin_state(quiet_until=None, quiet_reason=None)
        return None
    return quiet_until


def _parse_chinese_amount(raw: str) -> float:
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    if raw == "半":
        return 0.5
    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if raw == "十":
        return 10
    if "十" in raw:
        left, right = raw.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return float(tens * 10 + ones)
    if raw in digits:
        return float(digits[raw])
    raise ValueError(f"unknown amount: {raw}")


def _parse_reminder_text(text: str) -> tuple[datetime, str] | None:
    import re

    clean = text.strip()
    now = _local_now()

    def parse_amount(raw: str) -> float:
        return _parse_chinese_amount(raw)

    def parse_date() -> tuple[date | None, bool]:
        return _parse_date_hint(clean, now)

    relative = re.search(r"(\d+|[零一二两三四五六七八九十]+|半)\s*(分钟|分|小时|个小时|h|m)\s*后", clean, re.I)
    if relative:
        amount = parse_amount(relative.group(1))
        unit = relative.group(2)
        delta = timedelta(hours=amount) if unit in {"小时", "个小时", "h"} else timedelta(minutes=amount)
        reminder_text = clean[relative.end() :].strip(" ，,。")
        reminder_text = re.sub(r"^(叫我|提醒我|喊我|催我)", "", reminder_text).strip(" ，,。")
        return now + delta, reminder_text or "时间到了。"

    time_pattern = re.compile(r"(上午|早上|中午|下午|晚上|今晚|今天)?\s*(\d{1,2})\s*(?::|：|点)\s*(半|\d{0,2})")
    absolute_matches = list(time_pattern.finditer(clean))
    if absolute_matches and re.search(r"提醒|叫我|喊我|催我", clean):
        candidates: list[tuple[int, datetime, str]] = []
        single_time = len(absolute_matches) == 1
        for absolute in absolute_matches:
            clause_start = max(clean.rfind(mark, 0, absolute.start()) for mark in ("，", ",", "。", "；", ";", "\n")) + 1
            next_marks = [pos for mark in ("，", ",", "。", "；", ";", "\n") if (pos := clean.find(mark, absolute.end())) != -1]
            clause_end = min(next_marks) if next_marks else len(clean)
            clause = clean[clause_start:clause_end].strip()
            if not re.search(r"提醒|叫我|喊我|催我", clause):
                continue

            target_date, has_explicit_date = _parse_date_hint(clause, now)
            if not has_explicit_date and single_time:
                target_date, has_explicit_date = parse_date()
            if has_explicit_date and target_date is None:
                continue

            period = absolute.group(1) or ""
            hour = int(absolute.group(2))
            minute_raw = absolute.group(3) or "0"
            minute = 30 if minute_raw == "半" else int(minute_raw or "0")
            if period == "中午" and hour < 11:
                hour += 12
            if period in {"下午", "晚上", "今晚"} and hour < 12:
                hour += 12
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                continue

            date_for_run = target_date or now.date()
            run_at = now.replace(
                year=date_for_run.year,
                month=date_for_run.month,
                day=date_for_run.day,
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if not has_explicit_date and run_at <= now:
                run_at += timedelta(days=1)
            if run_at <= now:
                continue

            local_end = absolute.end() - clause_start
            reminder_text = clause[local_end:].strip(" ，,。")
            reminder_text = re.sub(r"^(之前|左右|的时候)?\s*(提醒我|叫我|喊我|催我)", "", reminder_text).strip(" ，,。")
            score = 2 if has_explicit_date else 1
            candidates.append((score, run_at, reminder_text or "时间到了。"))

        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return candidates[0][1], candidates[0][2]

    return None


def _parse_clock_time(text: str) -> tuple[int, int] | None:
    numeric = re.search(r"(上午|早上|中午|下午|晚上|今晚|今夜|夜|晚)?\s*(\d{1,2})\s*(?::|：|点)\s*(半|\d{0,2})", text)
    if numeric:
        period = numeric.group(1) or ""
        hour = int(numeric.group(2))
        minute_raw = numeric.group(3) or "0"
        minute = 30 if minute_raw == "半" else int(minute_raw or "0")
        if period == "中午" and hour < 11:
            hour += 12
        if period in {"下午", "晚上", "今晚", "今夜", "夜", "晚"} and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute

    chinese = re.search(r"(上午|早上|中午|下午|晚上|今晚|今夜|夜|晚)?\s*([零一二两三四五六七八九十]{1,3})点(半)?", text)
    if chinese:
        period = chinese.group(1) or ""
        try:
            hour = int(_parse_chinese_amount(chinese.group(2)))
        except ValueError:
            return None
        minute = 30 if chinese.group(3) else 0
        if period == "中午" and hour < 11:
            hour += 12
        if period in {"下午", "晚上", "今晚", "今夜", "夜", "晚"} and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None


def _next_daily_run(hour: int, minute: int, after: datetime | None = None) -> datetime:
    now = after or _local_now()
    run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return run_at


def _parse_daily_reminder_text(text: str) -> tuple[datetime, str, str] | None:
    clean = text.strip()
    if not re.search(r"每天|每日|每晚|每天晚上|毎日|毎晩", clean):
        return None
    if not re.search(r"提醒|叫我|喊我|催我|リマインド", clean):
        return None
    parsed_time = _parse_clock_time(clean)
    if not parsed_time:
        return None
    hour, minute = parsed_time
    reminder_text = clean
    for pattern in (
        r"^/remind\s*",
        r"每天晚上?\s*",
        r"每晚\s*",
        r"每天\s*",
        r"每日\s*",
        r"毎日\s*",
        r"毎晩\s*",
        r"(上午|早上|中午|下午|晚上|今晚|今夜|夜|晚)?\s*(\d{1,2})\s*(?::|：|点)\s*(半|\d{0,2})",
        r"(上午|早上|中午|下午|晚上|今晚|今夜|夜|晚)?\s*([零一二两三四五六七八九十]{1,3})点(半)?",
        r"^(提醒我|叫我|喊我|催我|リマインド)",
    ):
        reminder_text = re.sub(pattern, "", reminder_text).strip(" ，,。")
    if "画画" in clean and ("打卡" in clean or "课" in clean):
        reminder_text = "画画打卡"
    reminder_text = reminder_text or "时间到了。"
    time_label = f"{hour:02d}:{minute:02d}"
    return _next_daily_run(hour, minute), reminder_text, time_label


def _format_reminder_time(run_at: datetime) -> str:
    now = _local_now()
    if run_at.date() == now.date():
        return run_at.strftime("%H:%M")
    if run_at.year == now.year:
        return run_at.strftime("%m月%d日 %H:%M").lstrip("0").replace("月0", "月")
    return run_at.strftime("%Y年%m月%d日 %H:%M").replace("年0", "年").replace("月0", "月")


def _reminder_set_text(run_at: datetime, text: str) -> str:
    return f"了解。{_format_reminder_time(run_at)}に「{text}」ってリマインドする。\n[[ZH:知道了。{_format_reminder_time(run_at)} 提醒你：{text}。]]"


def _daily_reminder_set_text(time_label: str, text: str) -> str:
    return f"了解。毎日{time_label}に「{text}」ってリマインドする。\n[[ZH:知道了。每天 {time_label} 提醒你：{text}。]]"


def _reminder_fire_text(text: str) -> str:
    return f"リマインド：{text}\n[[ZH:提醒：{text}]]"


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data or {}
    chat_id = job_data.get("chat_id")
    text = job_data.get("text") or "时间到了。"
    reminder_id = job_data.get("id")
    run_at = job_data.get("run_at") or _local_now().isoformat()
    fired_at = _local_now().strftime("%Y-%m-%d %H:%M")
    if chat_id:
        message = _reminder_fire_text(text)
        _remember(chat_id, "bot", f"[提醒已在 {fired_at} 触发；原定时间 {run_at}] {message}")
        try:
            vault.append_life_event(f"[提醒触发] {text}（{fired_at}）", "reminder")
        except Exception:
            logging.exception("reminder life append failed")
        await context.bot.send_message(chat_id=chat_id, text=_telegram_html(message[:3900]), parse_mode="HTML")
        _mark_proactive("reminder", message)
    if reminder_id:
        reminders = [item for item in _load_reminders_state() if item.get("id") != reminder_id]
        if job_data.get("repeat") == "daily":
            try:
                hour, minute = [int(part) for part in str(job_data.get("time", "00:00")).split(":", 1)]
            except Exception:
                hour, minute = 0, 0
            next_run = _next_daily_run(hour, minute)
            job_data["run_at"] = next_run.isoformat()
            reminders.append(dict(job_data))
            delay = max(1, (next_run - _local_now()).total_seconds())
            context.job_queue.run_once(
                reminder_job,
                when=delay,
                data=dict(job_data),
                name=f"reminder-{reminder_id}",
                job_kwargs=JOB_KWARGS,
            )
        _save_reminders_state(reminders)


async def quiet_followup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data or {}
    chat_id = job_data.get("chat_id")
    reason = job_data.get("reason") or _load_checkin_state().get("quiet_reason") or "可联系时间到了"
    if not chat_id or not _load_checkin_state().get("enabled"):
        return
    state = _load_checkin_state()
    quiet_raw = state.get("quiet_until")
    try:
        quiet_until = datetime.fromisoformat(quiet_raw) if quiet_raw else None
    except Exception:
        quiet_until = None
    if quiet_until and quiet_until > _local_now():
        _schedule_quiet_followup(context.job_queue, quiet_until, reason)
        return
    _update_checkin_state(quiet_until=None, quiet_reason=None)
    try:
        text = await llm.ask(
            prompts.quiet_followup_prompt(reason, _relationship_text(chat_id), _life_context()),
            model=VOICE_MODEL,
        )
    except Exception:
        logging.exception("quiet followup failed")
        text = "時間になった。終わった？ ご飯はまだなら先に食べて。[[ZH:时间到了。结束了吗？还没吃饭的话先去吃。]]"
    _remember(chat_id, "bot", text)
    try:
        vault.append_life_event(f"[到点回访] {reason}", "checkin")
    except Exception:
        logging.exception("quiet followup life append failed")
    await context.bot.send_message(chat_id=chat_id, text=_telegram_html(text[:3900]), parse_mode="HTML")
    _mark_proactive("quiet_followup", text)


def _spark_should_skip(chat_id: str) -> str | None:
    if not _load_checkin_state().get("enabled"):
        return "checkin disabled"
    if _checkin_quiet_until():
        return "quiet_until active"
    if CHAT_INPUT_BUFFERS.get(_chat_key(chat_id)):
        return "user input pending"

    now = datetime.now(timezone.utc)
    last_user = _last_user_seen(chat_id)
    if last_user is None:
        return "no user history"
    idle_seconds = (now - last_user.astimezone(timezone.utc)).total_seconds()
    if idle_seconds < SPARK_MIN_IDLE_SEC:
        return f"user idle only {idle_seconds:.0f}s"

    last_proactive = _last_proactive_at()
    if last_proactive is not None:
        cooldown = (now - last_proactive.astimezone(timezone.utc)).total_seconds()
        if cooldown < SPARK_COOLDOWN_SEC:
            return f"cooldown {cooldown:.0f}s"
    return None


def _parse_spark_answer(answer: str) -> str | None:
    clean = (answer or "").strip()
    if not clean:
        return None
    first, _, rest = clean.partition("\n")
    marker = first.strip().upper()
    if marker.startswith("SKIP"):
        return None
    if marker.startswith("SEND"):
        message = rest.strip()
        return message or None
    return clean


def _n1_repetition_risk(chat_id: int | str | None, candidate: str) -> bool:
    if chat_id is None or not re.search(r"\bN1\b|Ｎ１", candidate or "", re.I):
        return False
    turns = _load_chat_history().get(str(chat_id), [])[-16:]
    n1_questions = 0
    last_n1_question_index: int | None = None
    for index, turn in enumerate(turns):
        text = turn.get("text", "")
        if turn.get("role") == "bot" and re.search(r"\bN1\b|Ｎ１", text, re.I) and re.search(
            r"どうだった|怎么样|どう|結果|分|考|試験|考试|\?|？",
            text,
        ):
            n1_questions += 1
            last_n1_question_index = index
    if n1_questions >= 2:
        return True
    if last_n1_question_index is not None:
        after_question = turns[last_n1_question_index + 1 :]
        for turn in after_question:
            if turn.get("role") != "user":
                continue
            text = turn.get("text", "")
            if re.search(r"不知道|不清楚|不晓得|わからない|分からない|もういい|别问|不要问|不问|到家|帰った", text):
                return True
    return False


def _sanitize_proactive_text(chat_id: int | str | None, text: str) -> str:
    if _n1_repetition_risk(chat_id, text):
        return "お疲れ。N1のことは今は聞かない。水飲んで、今日は休め。\n[[ZH:辛苦了。N1现在不问。喝点水，今天休息。]]"
    return text


async def spark_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = os.getenv("CHAT_ID") or (CHAT_ID_FILE.read_text(encoding="utf-8").strip() if CHAT_ID_FILE.exists() else "")
    if not chat_id:
        return
    skip_reason = _spark_should_skip(str(chat_id))
    if skip_reason:
        logging.info("spark skipped: %s", skip_reason)
        return

    todos = "\n".join(vault.open_todos()[:12])
    try:
        answer = await llm.ask(
            prompts.spark_prompt(_context_for(chat_id), todos, _relationship_text(chat_id), _life_context()),
            model=VOICE_MODEL,
        )
        text = _parse_spark_answer(answer)
    except Exception:
        logging.exception("spark failed")
        return
    if not text:
        logging.info("spark decided to skip")
        return
    text = _sanitize_proactive_text(chat_id, text)
    _remember(chat_id, "bot", text)
    try:
        vault.append_life_event(f"[spark主动消息] {text[:120]}", "checkin")
    except Exception:
        logging.exception("spark life append failed")
    await context.bot.send_message(chat_id=chat_id, text=_telegram_html(text[:3900]), parse_mode="HTML")
    _mark_proactive("spark", text)


def _schedule_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, run_at: datetime, text: str) -> dict:
    reminder_id = f"{chat_id}-{int(run_at.timestamp())}-{len(_load_reminders_state())}"
    reminder = {
        "id": reminder_id,
        "chat_id": str(chat_id),
        "run_at": run_at.isoformat(),
        "text": text,
    }
    reminders = _load_reminders_state()
    reminders.append(reminder)
    _save_reminders_state(reminders)
    delay = max(1, (run_at - _local_now()).total_seconds())
    context.job_queue.run_once(
        reminder_job,
        when=delay,
        data=reminder,
        name=f"reminder-{reminder_id}",
        job_kwargs=JOB_KWARGS,
    )
    return reminder


def _schedule_daily_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, run_at: datetime, text: str, time_label: str) -> dict:
    reminder_key = abs(sum(ord(ch) for ch in text))
    reminder_id = f"daily-{chat_id}-{time_label.replace(':', '')}-{reminder_key}"
    reminder = {
        "id": reminder_id,
        "chat_id": str(chat_id),
        "run_at": run_at.isoformat(),
        "text": text,
        "repeat": "daily",
        "time": time_label,
    }
    reminders = [item for item in _load_reminders_state() if item.get("id") != reminder_id]
    reminders.append(reminder)
    _save_reminders_state(reminders)
    delay = max(1, (run_at - _local_now()).total_seconds())
    context.job_queue.run_once(
        reminder_job,
        when=delay,
        data=reminder,
        name=f"reminder-{reminder_id}",
        job_kwargs=JOB_KWARGS,
    )
    return reminder


def _restore_reminders(app: Application) -> None:
    now = _local_now()
    active: list[dict] = []
    for reminder in _load_reminders_state():
        try:
            run_at = datetime.fromisoformat(reminder["run_at"])
        except Exception:
            continue
        if run_at <= now:
            if reminder.get("repeat") != "daily":
                continue
            try:
                hour, minute = [int(part) for part in str(reminder.get("time", "00:00")).split(":", 1)]
            except Exception:
                continue
            run_at = _next_daily_run(hour, minute, now)
            reminder["run_at"] = run_at.isoformat()
        delay = max(1, (run_at - now).total_seconds())
        app.job_queue.run_once(
            reminder_job,
            when=delay,
            data=reminder,
            name=f"reminder-{reminder.get('id')}",
            job_kwargs=JOB_KWARGS,
        )
        active.append(reminder)
    _save_reminders_state(active)


def _restore_quiet_followup(app: Application) -> None:
    state = _load_checkin_state()
    if not state.get("enabled"):
        return
    raw = state.get("quiet_until")
    if not raw:
        return
    try:
        quiet_until = datetime.fromisoformat(raw)
    except Exception:
        _update_checkin_state(quiet_until=None, quiet_reason=None)
        return
    if quiet_until <= _local_now():
        delay = 1
    else:
        delay = max(1, (quiet_until - _local_now()).total_seconds())
    chat_id = os.getenv("CHAT_ID") or (CHAT_ID_FILE.read_text(encoding="utf-8").strip() if CHAT_ID_FILE.exists() else "")
    if not chat_id:
        return
    app.job_queue.run_once(
        quiet_followup_job,
        when=delay,
        data={"chat_id": str(chat_id), "quiet_until": quiet_until.isoformat(), "reason": state.get("quiet_reason") or ""},
        name=_quiet_followup_job_name(),
        job_kwargs=JOB_KWARGS,
    )


async def checkin_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _update_checkin_state(enabled=True)
    quiet_until = _checkin_quiet_until()
    if quiet_until:
        _schedule_quiet_followup(context.job_queue, quiet_until, _load_checkin_state().get("quiet_reason") or "")
    await _reply(update, "了解。これから約2時間ごとに様子を見る。")


async def checkin_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _update_checkin_state(enabled=False)
    await _reply(update, "了解。定期チェックは止めた。")


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip()
    if not text:
        await _reply(update, "用法：/remind 10分钟后 叫我洗澡\n或：/remind 17:00 提醒我去取钱\n或：/remind 每天20:00 提醒我画画打卡")
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    parsed_daily = _parse_daily_reminder_text(text)
    if chat_id is not None and parsed_daily is not None:
        run_at, reminder_text, time_label = parsed_daily
        _schedule_daily_reminder(context, chat_id, run_at, reminder_text, time_label)
        await _reply(update, _daily_reminder_set_text(time_label, reminder_text))
        return
    parsed = _parse_reminder_text(text)
    if chat_id is None or parsed is None:
        await _reply(update, "我没读懂时间。你可以说：/remind 10分钟后 叫我洗澡，/remind 17:00 提醒我去取钱，或 /remind 每天20:00 提醒我画画打卡。")
        return
    run_at, reminder_text = parsed
    _schedule_reminder(context, chat_id, run_at, reminder_text)
    await _reply(update, _reminder_set_text(run_at, reminder_text))


async def checkin_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _load_checkin_state().get("enabled"):
        return
    quiet_until = _checkin_quiet_until()
    if quiet_until:
        _schedule_quiet_followup(context.job_queue, quiet_until, _load_checkin_state().get("quiet_reason") or "")
        logging.info("checkin skipped until %s", quiet_until.isoformat())
        return
    chat_id = os.getenv("CHAT_ID") or (CHAT_ID_FILE.read_text(encoding="utf-8").strip() if CHAT_ID_FILE.exists() else "")
    if not chat_id:
        return
    now = datetime.now(timezone.utc)
    last_user = _last_user_seen(chat_id)
    if last_user is None:
        logging.info("checkin skipped: no user history")
        return
    idle_seconds = (now - last_user.astimezone(timezone.utc)).total_seconds()
    if idle_seconds < IDLE_CHECKIN_MIN_IDLE_SEC:
        logging.info("checkin skipped: user idle only %.0fs", idle_seconds)
        return
    last_proactive = _last_proactive_at()
    if last_proactive is not None:
        cooldown = (now - last_proactive.astimezone(timezone.utc)).total_seconds()
        if cooldown < IDLE_CHECKIN_COOLDOWN_SEC:
            logging.info("checkin skipped: proactive cooldown %.0fs", cooldown)
            return
    idle_note = (
        f"用户已经约 {_format_duration_ja(idle_seconds)} 没有回复。"
        "这次消息的目的只是确认状态，不要责怪，不要说用户冷落你。"
    )
    try:
        text = await llm.ask(
            prompts.checkin_prompt(_relationship_text(chat_id), _life_context(), _context_for(chat_id), idle_note),
            model=VOICE_MODEL,
        )
    except Exception:
        logging.exception("checkin failed")
        text = f"{_format_duration_ja(idle_seconds)}くらい空いた。今どう。水だけ飲んで、生存報告。\n[[ZH:大概过了{_format_duration_ja(idle_seconds)}没回。现在怎么样？先喝点水，报个平安。]]"
    text = _sanitize_proactive_text(chat_id, text)
    await context.bot.send_message(chat_id=chat_id, text=_telegram_html(text[:3900]), parse_mode="HTML")
    _remember(chat_id, "bot", text)
    _mark_proactive("checkin", text)


async def remember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip()
    if not text:
        await _reply(update, "用法：/remember 用户喜欢我说话短一点")
        return
    _append_memory(text)
    await _reply(update, "记进可塑层了。")


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, _read_memory()[:3900])


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, _read_user_profile()[:3900])


async def profile_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip()
    if not text:
        await _reply(update, "用法：/profile_add 我目前在日本读书，研究方向是……")
        return
    _append_user_profile(text)
    await _reply(update, "记进用户基础档案了。")


async def life_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    days = 3
    if context.args:
        try:
            days = max(1, min(14, int(context.args[0])))
        except ValueError:
            days = 3
    events = vault.recent_life_events(days=days)
    if not events:
        await _reply(update, "还没有日常生活记录。")
        return
    event_text = "\n".join(f"- {item['time']} [{item['source']}] {item['text']}" for item in events)[-12000:]
    try:
        summary = await llm.ask(
            prompts.life_summary_prompt(event_text, _relationship_text(chat_id)),
            timeout=int(os.getenv("CLAUDE_FALLBACK_TIMEOUT_SEC", "45")),
            model=VOICE_MODEL,
        )
    except Exception:
        logging.exception("life summary failed")
        summary = vault.rough_life_summary(days=days)
    await _reply(update, summary[:3900])


async def familiarity_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    await _reply(update, _relationship_text(chat_id))


def _chat_key(chat_id: int | str | None) -> str:
    return str(chat_id or "unknown")


def _drain_chat_buffer(key: str) -> list[str]:
    items = CHAT_INPUT_BUFFERS.get(key, [])
    CHAT_INPUT_BUFFERS[key] = []
    return [item for item in items if item.strip()]


def _short_context(chat_id: int | str | None, max_lines: int = 8) -> str:
    context = _context_for(chat_id)
    lines = [line for line in context.splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


def _looks_like_web_request(text: str) -> bool:
    lowered = text.lower()
    keywords = [
        "搜索",
        "搜一下",
        "查一下",
        "查找",
        "帮我查",
        "调べて",
        "調べて",
        "検索",
        "ググ",
        "web",
        "官网",
        "官方",
        "最新",
        "今年",
        "2025",
        "2026",
        "上伊那牡丹",
    ]
    return any(keyword.lower() in lowered for keyword in keywords)


async def _ask_with_fallback(prompt: str, fallback_prompt: str, label: str) -> str:
    try:
        return await llm.ask(prompt, model=VOICE_MODEL)
    except Exception as first_error:
        logging.warning("%s failed, retrying with compact prompt: %s", label, first_error)
        try:
            return await llm.ask(fallback_prompt, timeout=int(os.getenv("CLAUDE_FALLBACK_TIMEOUT_SEC", "45")), model=VOICE_MODEL)
        except Exception as second_error:
            raise RuntimeError(f"{second_error}；已尝试短上下文重试。原始错误：{first_error}") from second_error


async def _generate_message_reply(chat_id: int | str | None, text: str) -> tuple[str, str, str | None, list[str]]:
    recent_context = _context_for(chat_id)
    life_context = _life_context()
    relationship = _relationship_text(chat_id)
    compact_context = _short_context(chat_id)
    if _looks_like_web_request(text):
        try:
            reply = await llm.ask_web(
                prompts.web_search_prompt(text, recent_context, relationship, life_context),
                timeout=int(os.getenv("CLAUDE_WEB_TIMEOUT_SEC", "120")),
                model=VOICE_MODEL,
            )
            return "提问", reply, None, []
        except Exception as web_error:
            logging.warning("web search failed, falling back to normal chat: %s", web_error)
            reply = await _ask_with_fallback(
                prompts.free_chat_prompt(
                    f"{text}\n\n[系统提示] 刚才尝试联网搜索失败：{web_error}。请不要编造，直接说明没查到，并建议用户提供更多线索。",
                    recent_context,
                    relationship,
                    life_context,
                ),
                prompts.free_chat_prompt(text, compact_context, relationship, ""),
                "web fallback chat",
            )
            return "提问", reply, None, []
    try:
        raw = await _ask_with_fallback(
            prompts.chat_prompt(text, recent_context, relationship, life_context),
            prompts.chat_prompt(text, compact_context, relationship, ""),
            "intent classifier",
        )
    except Exception as classifier_error:
        logging.warning("intent classifier failed, falling back to free chat: %s", classifier_error)
        reply = await _ask_with_fallback(
            prompts.free_chat_prompt(text, recent_context, relationship, life_context),
            prompts.free_chat_prompt(text, compact_context, relationship, ""),
            "free chat after classifier failure",
        )
        return "其他", reply, None, []
    data = llm.first_json(raw)
    intent = data.get("intent") or "其他"
    reply = data.get("reply") or ""
    due = data.get("due")
    todos = data.get("todos") or []
    if not isinstance(todos, list):
        todos = []
    todos = [str(item).strip() for item in todos if str(item).strip()]
    if not reply:
        reply = await _ask_with_fallback(
            prompts.free_chat_prompt(text, recent_context, relationship, life_context),
            prompts.free_chat_prompt(text, compact_context, relationship, ""),
            "free chat",
        )
    return intent, reply, due, todos


async def _regenerate_after_interrupt(
    chat_id: int | str | None,
    combined_text: str,
    draft_reply: str,
    latest_addition: str,
) -> tuple[str, str, None, list[str]]:
    prompt = prompts.interrupted_chat_prompt(
        combined_text,
        draft_reply,
        latest_addition,
        _context_for(chat_id),
        _relationship_text(chat_id),
        _life_context(),
    )
    fallback_prompt = prompts.interrupted_chat_prompt(
        combined_text[-6000:],
        draft_reply[-2000:],
        latest_addition,
        _short_context(chat_id),
        _relationship_text(chat_id),
        "",
    )
    reply = await _ask_with_fallback(prompt, fallback_prompt, "interrupt regeneration")
    return "其他", reply, None, []


async def _conversation_worker(key: str) -> None:
    try:
        while True:
            await asyncio.sleep(max(0.1, int(os.getenv("BATCH_DEBOUNCE_SEC", "3"))))
            batch = _drain_chat_buffer(key)
            if not batch:
                return

            update = CHAT_LAST_UPDATE.get(key)
            if update is None:
                return
            chat_id = update.effective_chat.id if update.effective_chat else None
            combined_text = "\n".join(batch)
            intent = "其他"
            due = None
            todos: list[str] = []
            try:
                if update.message:
                    await update.message.chat.send_action(ChatAction.TYPING)
                intent, reply, due, todos = await _generate_message_reply(chat_id, combined_text)

                while CHAT_INPUT_BUFFERS.get(key):
                    await asyncio.sleep(max(0.1, int(os.getenv("INTERRUPT_DEBOUNCE_SEC", "2"))))
                    additions = _drain_chat_buffer(key)
                    if not additions:
                        break
                    latest_addition = "\n".join(additions)
                    combined_text = f"{combined_text}\n\n[用户补充/打断]\n{latest_addition}"
                    if update.message:
                        await update.message.chat.send_action(ChatAction.TYPING)
                    intent, reply, due, todos = await _regenerate_after_interrupt(chat_id, combined_text, reply, latest_addition)

                completed = vault.auto_complete_from_text(combined_text)
                vault.append_note(intent, combined_text, reply, due, todos=todos)
                if completed and "完" not in reply and "取" not in reply and "チェック" not in reply:
                    reply = f"{reply}\n\n完了にした：{completed}件\n[[ZH:已帮你勾掉 {completed} 条待办。]]"
                _remember(chat_id, "bot", reply)
                await _reply(update, reply)
            except Exception as e:
                logging.exception("message worker failed")
                vault.append_note("其他", combined_text)
                error_text = str(e)
                if "session limit" in error_text.lower() or "resets" in error_text.lower():
                    reply = "今ちょっと制限。Claude が 1:20am まで戻らない。\n\nでも記録はした。リマインドと定期チェックは動く。"
                else:
                    reply = f"我先帮你记下来了，但调用 Claude 时出错：{e}"
                _remember(chat_id, "bot", reply)
                await _reply(update, reply)
    finally:
        CHAT_WORKERS.pop(key, None)
        if CHAT_INPUT_BUFFERS.get(key):
            update = CHAT_LAST_UPDATE.get(key)
            if update and update.effective_chat:
                new_key = _chat_key(update.effective_chat.id)
                CHAT_WORKERS[new_key] = asyncio.create_task(_conversation_worker(new_key))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    chat_id = update.effective_chat.id if update.effective_chat else None
    key = _chat_key(chat_id)
    _maybe_set_quiet_until(text, context.job_queue)
    parsed_daily_reminder = _parse_daily_reminder_text(text)
    if chat_id is not None and parsed_daily_reminder:
        run_at, reminder_text, time_label = parsed_daily_reminder
        _schedule_daily_reminder(context, chat_id, run_at, reminder_text, time_label)
        _touch_relationship(chat_id)
        _remember(chat_id, "user", text)
        _remember(chat_id, "bot", _daily_reminder_set_text(time_label, reminder_text))
        try:
            vault.append_life_event(text, "telegram")
        except Exception:
            logging.exception("life append failed")
        await _reply(update, _daily_reminder_set_text(time_label, reminder_text))
        return
    parsed_reminder = _parse_reminder_text(text)
    if chat_id is not None and parsed_reminder and re.search(r"提醒|叫我|喊我|催我", text):
        run_at, reminder_text = parsed_reminder
        _schedule_reminder(context, chat_id, run_at, reminder_text)
        _touch_relationship(chat_id)
        _remember(chat_id, "user", text)
        _remember(chat_id, "bot", _reminder_set_text(run_at, reminder_text))
        try:
            vault.append_life_event(text, "telegram")
        except Exception:
            logging.exception("life append failed")
        await _reply(update, _reminder_set_text(run_at, reminder_text))
        return
    _touch_relationship(chat_id)
    _remember(chat_id, "user", text)
    try:
        vault.append_life_event(text, "telegram")
    except Exception:
        logging.exception("life append failed")
    CHAT_INPUT_BUFFERS.setdefault(key, []).append(text)
    CHAT_LAST_UPDATE[key] = update
    CHAT_LAST_CONTEXT[key] = context
    if key not in CHAT_WORKERS or CHAT_WORKERS[key].done():
        CHAT_WORKERS[key] = asyncio.create_task(_conversation_worker(key))


def _safe_filename(name: str) -> str:
    keep = []
    for char in name:
        if char.isalnum() or char in ".-_":
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep).strip("._") or "image"


async def _download_telegram_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[Path, str]:
    if not update.message:
        raise RuntimeError("没有消息")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    if update.message.photo:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        path = MEDIA_DIR / f"{update.message.message_id}_{photo.file_unique_id}.jpg"
        await tg_file.download_to_drive(path)
        return path, update.message.caption or ""

    doc = update.message.document
    if doc:
        name = _safe_filename(doc.file_name or f"{doc.file_unique_id}.bin")
        suffix = Path(name).suffix.lower()
        mime = (doc.mime_type or "").lower()
        if not (mime.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}):
            raise RuntimeError("这个文件不像图片。请发照片、截图，或图片文件。")
        tg_file = await context.bot.get_file(doc.file_id)
        path = MEDIA_DIR / f"{update.message.message_id}_{name}"
        await tg_file.download_to_drive(path)
        return path, update.message.caption or ""

    raise RuntimeError("没有收到图片。")


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    _touch_relationship(chat_id)
    try:
        path, caption = await _download_telegram_file(update, context)
        recent_context = _context_for(chat_id)
        prompt = prompts.image_prompt(str(path.resolve()), caption, recent_context, _relationship_text(chat_id), _life_context())
        reply = await llm.ask_image(prompt, model=VOICE_MODEL)
    except Exception as e:
        logging.exception("image failed")
        reply = f"图片我收到了，但读取失败：{e}"
    if update.message:
        label = update.message.caption or "[图片]"
        _remember(chat_id, "user", label)
        vault.append_life_event(label, "image")
    _remember(chat_id, "bot", reply)
    await _reply(update, reply)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("word", word_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("gmail", gmail_cmd))
    app.add_handler(CommandHandler("gmail_check", gmail_cmd))
    app.add_handler(CommandHandler("gmail_on", gmail_on_cmd))
    app.add_handler(CommandHandler("gmail_off", gmail_off_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("checkin_on", checkin_on_cmd))
    app.add_handler(CommandHandler("checkin_off", checkin_off_cmd))
    app.add_handler(CommandHandler("reset_context", reset_context_cmd))
    app.add_handler(CommandHandler("remember", remember_cmd))
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("profile_add", profile_add_cmd))
    app.add_handler(CommandHandler("life", life_cmd))
    app.add_handler(CommandHandler("familiarity", familiarity_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(gmail_check_job, interval=GMAIL_CHECK_INTERVAL_SEC, first=20, job_kwargs=JOB_KWARGS)
    app.job_queue.run_repeating(checkin_job, interval=CHECKIN_INTERVAL_SEC, first=60, job_kwargs=JOB_KWARGS)
    app.job_queue.run_repeating(spark_job, interval=SPARK_INTERVAL_SEC, first=90, job_kwargs=JOB_KWARGS)
    _restore_reminders(app)
    _restore_quiet_followup(app)
    logging.info("DOCTOR bot starting")
    app.run_polling()


if __name__ == "__main__":
    main()
