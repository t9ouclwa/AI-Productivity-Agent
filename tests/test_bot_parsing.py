from datetime import datetime, timedelta, timezone

import bot


JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=JST)


def test_relative_reminder(monkeypatch) -> None:
    monkeypatch.setattr(bot, "_local_now", lambda: NOW)
    parsed = bot._parse_reminder_text("十分钟后提醒我喝水")
    assert parsed == (NOW + timedelta(minutes=10), "喝水")


def test_absolute_reminder_uses_explicit_date(monkeypatch) -> None:
    monkeypatch.setattr(bot, "_local_now", lambda: NOW)
    parsed = bot._parse_reminder_text("明天晚上8点提醒我交报告")
    assert parsed is not None
    run_at, text = parsed
    assert run_at == datetime(2026, 10, 7, 20, 0, tzinfo=JST)
    assert text == "交报告"


def test_daily_reminder(monkeypatch) -> None:
    monkeypatch.setattr(bot, "_local_now", lambda: NOW)
    parsed = bot._parse_daily_reminder_text("每天晚上8点提醒我复习")
    assert parsed is not None
    run_at, text, time_label = parsed
    assert run_at == datetime(2026, 10, 6, 20, 0, tzinfo=JST)
    assert text == "复习"
    assert time_label == "20:00"


def test_recurring_schedule_does_not_create_quiet_window(monkeypatch) -> None:
    monkeypatch.setattr(bot, "_local_now", lambda: NOW)
    assert bot._parse_quiet_until("我每周三下午上课") is None


def test_explicit_followup_creates_quiet_window(monkeypatch) -> None:
    monkeypatch.setattr(bot, "_local_now", lambda: NOW)
    assert bot._parse_quiet_until("我在开会，半小时后再来找我") == NOW + timedelta(minutes=30)


def test_telegram_html_preserves_translation_as_spoiler() -> None:
    rendered = bot._telegram_html("おはよう\n[[ZH:早上好 & 注意]]")
    assert rendered == "おはよう\n<tg-spoiler>早上好 &amp; 注意</tg-spoiler>"


def test_split_bubbles_keeps_translation_block_together() -> None:
    bubbles = bot._split_bubbles("おはよう\n[[ZH:早上好\n今天加油]]")
    assert bubbles == ["おはよう\n[[ZH:早上好\n今天加油]]"]
