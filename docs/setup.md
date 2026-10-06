# Setup Guide

## Prerequisites

- Python 3.11 or newer
- A Telegram bot token from BotFather
- Claude Code installed and authenticated
- An existing local folder to use as an Obsidian-compatible Markdown vault

Gmail support is optional and requires a Google Cloud OAuth desktop client.

## Install

```bash
git clone https://github.com/Tyouclwannie/AI-Productivity-Agent.git
cd AI-Productivity-Agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
TELEGRAM_BOT_TOKEN=your_bot_token
VAULT_DIR=/absolute/path/to/your/obsidian-vault
```

Never commit `.env`, OAuth files, chat IDs, state files, or vault content.

## Configure Claude

Confirm that the same shell which starts DOCTOR can use Claude Code:

```bash
claude auth status
claude -p --model claude-sonnet-4-6 "Reply with OK"
```

If your installation uses a different executable or model, update `CLAUDE_BIN`, `CLAUDE_MODEL`, and `VOICE_MODEL` in `.env`.

## Optional personality and memory files

DOCTOR reads editable Markdown files from `~/.config/doctor/`:

```text
~/.config/doctor/
├── persona.md
├── memory.md
└── user_profile.md
```

Starter files are provided under [`config/`](../config/). These files are local user data and should not be committed after customization.

## Run

```bash
./run.sh
```

The ready state includes both `DOCTOR bot starting` and `Application started` in the logs. Ensure only one instance polls Telegram at a time; duplicate instances produce a `getUpdates` conflict.

## Optional Gmail integration

1. Create an OAuth 2.0 Desktop application in Google Cloud.
2. Enable the Gmail API.
3. Download the client file as `credentials.json` into the project root.
4. Run `python gmail_client.py` and complete the browser authorization.

DOCTOR requests the read-only `gmail.readonly` scope. The generated `token.json` is a credential and must remain private.

## Test

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q bot.py gmail_client.py llm.py prompts.py vault.py scripts
python -m pytest
```

The automated suite does not call Telegram, Gmail, or Claude. Use a private test bot and non-sensitive sample data for end-to-end checks.
