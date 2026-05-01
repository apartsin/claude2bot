"""PostToolUse hook: stream intermediate assistant content to Telegram as
each tool call boundary creates a new transcript entry.

Without this, the regular Stop-hook mirror only fires once per turn and
shows the full response after Claude finishes. This hook fires after EVERY
tool call, so the user sees text/tools/thinking arrive in real time.

Cursor: ~/.claude/hooks/.last_mirrored_uuid stores the uuid of the most
recently mirrored assistant entry. We only emit entries newer than that.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HOOKS_DIR = Path.home() / ".claude" / "hooks"
QUEUE_DIR = HOOKS_DIR / "telegram_queue"
CURSOR_FILE = HOOKS_DIR / ".last_mirrored_uuid"
LOG_FILE = HOOKS_DIR / "telegram_stream.log"
MAX_BLOCK_CHARS = 600        # truncate per block to keep messages compact
MAX_TOTAL_CHARS = 3000


def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] stream[{os.getpid()}] {msg}\n")
    except Exception:
        pass


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def read_cursor() -> str:
    if CURSOR_FILE.exists():
        try:
            return CURSOR_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


def write_cursor(uuid: str) -> None:
    try:
        CURSOR_FILE.write_text(uuid, encoding="utf-8")
    except Exception:
        pass


def extract_session_label(transcript_path: str) -> str:
    """Most recent customTitle or slug; fallback to '?'"""
    if not transcript_path or not os.path.exists(transcript_path):
        return "?"
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            if '"customTitle"' in line:
                try:
                    obj = json.loads(line.strip())
                    if obj.get("customTitle"):
                        return obj["customTitle"]
                except Exception:
                    continue
        for line in reversed(lines):
            if '"slug"' in line:
                try:
                    obj = json.loads(line.strip())
                    if obj.get("slug"):
                        return obj["slug"]
                except Exception:
                    continue
    except Exception:
        pass
    return "?"


def render_block(block: dict) -> str | None:
    """Format one content block as HTML for Telegram."""
    btype = block.get("type")
    if btype == "text":
        text = block.get("text", "")
        if not text.strip():
            return None
        return html_escape(text[:MAX_BLOCK_CHARS])
    if btype == "tool_use":
        name = block.get("name", "tool")
        inp = block.get("input") or {}
        # Compact one-line description of important fields
        preview = ""
        if isinstance(inp, dict):
            for key in ("file_path", "command", "pattern", "url", "prompt", "description"):
                if key in inp and isinstance(inp[key], str):
                    preview = inp[key][:120]
                    break
            if not preview:
                preview = json.dumps(inp)[:120]
        return f"🔧 <code>{html_escape(name)}</code> {html_escape(preview)}"
    if btype == "thinking":
        text = block.get("thinking", "")
        if not text.strip():
            return None
        return f"💭 <i>{html_escape(text[:MAX_BLOCK_CHARS])}</i>"
    if btype == "tool_result":
        # Skip — these are user-side blocks
        return None
    return None


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        log(f"bad stdin: {e}")
        return

    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        log("no transcript")
        return

    cursor = read_cursor()

    # Read all assistant entries since cursor (forward-chronological).
    new_entries: list[dict] = []
    try:
        with open(transcript, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        log(f"read fail: {e}")
        return

    found_cursor = (cursor == "")  # if no cursor, all are new
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        uuid = obj.get("uuid", "")
        if not found_cursor:
            if uuid == cursor:
                found_cursor = True
            continue
        new_entries.append(obj)

    if not new_entries:
        return

    # Build output: walk entries, render every content block.
    cwd = payload.get("cwd") or ""
    project = os.path.basename(cwd.rstrip("/\\")) if cwd else "?"
    session_label = extract_session_label(transcript)
    label = f"{project}/{session_label}"
    ts = datetime.now().strftime("%H:%M")

    parts = [f"🤖 <b>[{ts}] {html_escape(label)}</b> <i>(stream)</i>"]
    total = len(parts[0])
    last_uuid = cursor
    for entry in new_entries:
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                rendered = render_block(block)
                if rendered:
                    if total + len(rendered) > MAX_TOTAL_CHARS:
                        parts.append("…")
                        break
                    parts.append(rendered)
                    total += len(rendered) + 1
            else:
                last_uuid = entry.get("uuid", last_uuid)
                continue
            last_uuid = entry.get("uuid", last_uuid)
            break  # hit length limit
        last_uuid = entry.get("uuid", last_uuid)

    if len(parts) <= 1:
        # No renderable content (e.g. all tool_result entries)
        write_cursor(last_uuid)
        return

    body = "\n\n".join(parts)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{time.time():.6f}-stream.json"
    payload_out = {"text": body[:4000], "parse_mode": "HTML"}
    (QUEUE_DIR / fname).write_text(json.dumps(payload_out), encoding="utf-8")
    write_cursor(last_uuid)
    log(f"streamed {len(new_entries)} entries, {total} chars, cursor → {last_uuid[:8]}")


if __name__ == "__main__":
    main()
