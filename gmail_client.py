from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"


class GmailNotReady(RuntimeError):
    pass


def _credentials() -> Credentials:
    if not CREDENTIALS_FILE.exists():
        raise GmailNotReady("缺少 credentials.json，请将 Google OAuth 桌面客户端凭据放到项目根目录。")

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def service():
    return build("gmail", "v1", credentials=_credentials())


def _header(headers: list[dict[str, str]], name: str) -> str:
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def list_unread(max_results: int = 8) -> list[dict[str, Any]]:
    svc = service()
    result = (
        svc.users()
        .messages()
        .list(userId="me", q="is:unread newer_than:14d", maxResults=max_results)
        .execute()
    )
    messages = result.get("messages", [])
    items = []
    for msg in messages:
        data = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = data.get("payload", {}).get("headers", [])
        items.append(
            {
                "id": data.get("id", ""),
                "thread_id": data.get("threadId", ""),
                "from": _header(headers, "From"),
                "subject": _header(headers, "Subject") or "(no subject)",
                "date": _header(headers, "Date"),
                "snippet": data.get("snippet", ""),
            }
        )
    return items


def format_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "没有未读邮件。"
    blocks = []
    for index, msg in enumerate(messages, start=1):
        blocks.append(
            "\n".join(
                [
                    f"{index}. From: {msg['from']}",
                    f"Subject: {msg['subject']}",
                    f"Date: {msg['date']}",
                    f"Snippet: {msg['snippet']}",
                    f"ID: {msg['id']}",
                ]
            )
        )
    return "\n\n".join(blocks)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"enabled": False, "seen_ids": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"enabled": False, "seen_ids": []}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def authorize() -> None:
    _credentials()


if __name__ == "__main__":
    authorize()
    print("Gmail authorization OK")
