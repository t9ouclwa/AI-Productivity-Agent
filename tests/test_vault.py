from pathlib import Path

import pytest

import vault


def configure_vault(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(vault, "VAULT_DIR", root)


def test_missing_vault_dir_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(vault, "VAULT_DIR", None)
    with pytest.raises(RuntimeError, match="VAULT_DIR is not set"):
        vault.open_todos()


def test_todo_round_trip(monkeypatch, tmp_path: Path) -> None:
    configure_vault(monkeypatch, tmp_path)
    path = vault.append_todos(["提交报告", "复习日语"], due="2026-10-07")

    assert path.exists()
    assert len(vault.open_todos()) == 2
    assert vault.complete_todos("提交报告") == 1
    assert len(vault.open_todos()) == 1


def test_translation_marker_is_not_treated_as_obsidian_link(monkeypatch, tmp_path: Path) -> None:
    configure_vault(monkeypatch, tmp_path)
    path = tmp_path / "note.md"
    path.write_text("# Note\n\n[[ZH:隐藏翻译]]\n", encoding="utf-8")

    vault.ensure_links(path, ["[[DOCTOR总览]]"])

    text = path.read_text(encoding="utf-8")
    assert "[[ZH:隐藏翻译]]" in text
    assert text.count("[[DOCTOR总览]]") == 1
