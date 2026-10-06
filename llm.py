import asyncio
import json
import os
import re
from asyncio.subprocess import PIPE
from pathlib import Path


CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
VOICE_MODEL = os.getenv("VOICE_MODEL", "claude-sonnet-4-6")
_CWD = Path(__file__).resolve().parent


async def ask(prompt: str, timeout: int | None = None, model: str | None = None) -> str:
    timeout = timeout or int(os.getenv("CLAUDE_TIMEOUT_SEC", "75"))
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN,
        "-p",
        "--model",
        model or MODEL,
        prompt,
        stdout=PIPE,
        stderr=PIPE,
        cwd=_CWD,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("Claude timed out")
    if proc.returncode != 0:
        stderr = err.decode("utf-8", "replace").strip()
        stdout = out.decode("utf-8", "replace").strip()
        detail = stderr or stdout or f"Claude failed with exit code {proc.returncode}"
        raise RuntimeError(detail)
    return out.decode("utf-8", "replace").strip()


async def ask_web(prompt: str, timeout: int = 150, model: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN,
        "-p",
        "--model",
        model or VOICE_MODEL,
        prompt,
        "--allowedTools",
        "WebSearch",
        "WebFetch",
        stdout=PIPE,
        stderr=PIPE,
        cwd=_CWD,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace").strip() or "Claude failed")
    return out.decode("utf-8", "replace").strip()


async def ask_image(prompt: str, timeout: int = 180, model: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN,
        "-p",
        "--model",
        model or VOICE_MODEL,
        prompt,
        "--allowedTools",
        "Read",
        stdout=PIPE,
        stderr=PIPE,
        cwd=_CWD,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("Claude image read timed out")
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace").strip() or "Claude image read failed")
    return out.decode("utf-8", "replace").strip()


def first_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}
