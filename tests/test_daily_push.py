import subprocess
import sys
from pathlib import Path


def test_daily_push_resolves_project_imports() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(project_root / "scripts" / "daily_push.py")],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "TELEGRAM_BOT_TOKEN or CHAT_ID missing" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
