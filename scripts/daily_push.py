import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

import vault


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID") or (Path(".chat_id").read_text(encoding="utf-8").strip() if Path(".chat_id").exists() else "")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or CHAT_ID missing")
    todos = vault.open_todos()
    text = "早。今天没有未完成待办。" if not todos else "早。当前待办：\n" + "\n".join(todos[:20])
    await Bot(token).send_message(chat_id=chat_id, text=text)


if __name__ == "__main__":
    asyncio.run(main())
