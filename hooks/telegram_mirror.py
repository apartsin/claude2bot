"""Stop / UserPromptSubmit hook: enqueue Claude transcript messages for
delivery to Telegram via the background worker (telegram_worker.py).

Design:
  - This hook script must be FAST (the harness blocks on it). It only:
      1. Builds the message text (extracts from transcript or payload).
      2. Chunks if > Telegram's per-message limit.
      3. Writes each chunk as a JSON file to ~/.claude/hooks/telegram_queue/.
      4. Ensures the worker is running (forks a detached worker if not).
      5. Exits.
  - Actual HTTP delivery is the worker's job. So Claude conversation never
    waits on Telegram, and slow/flaky Telegram never blocks the harness.
  - Messages are persisted on disk until delivered, so worker death,
    network outages, restarts, etc. don't lose anything.
"""
import json
import os
import re
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime

HOOKS_DIR = Path.home() / ".claude" / "hooks"
QUEUE_DIR = HOOKS_DIR / "telegram_queue"
PID_FILE = HOOKS_DIR / "telegram_worker.pid"
WORKER_SCRIPT = HOOKS_DIR / "telegram_worker.py"
LOG_FILE = HOOKS_DIR / "telegram_mirror.log"
PYTHON = "/c/Python314/python"      # adjust if Python moves
WIN_PYTHON = r"C:\Python314\python.exe"  # absolute Windows path for Popen
PER_MESSAGE_LIMIT = 3500            # leave headroom under Telegram's 4096


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] hook[{os.getpid()}] {msg}\n")
    except Exception:
        pass


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_last_assistant_text(transcript_path):
    """Concatenate ALL assistant text blocks from the most recent turn.

    A single Claude turn can produce multiple assistant entries when tool
    calls interleave with text (text → tool_use → text → tool_use → text →
    final). Walk backwards from the end, collecting text blocks from every
    assistant entry until we hit a user/human entry — that boundary marks
    the start of the current turn.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    with open(transcript_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    collected = []  # list of (text) in reverse-chrono order
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t == "user":
            # Reached previous user turn boundary — stop scanning.
            if collected:
                break
            else:
                # Haven't found any assistant text yet; keep scanning past
                # this user turn (could be a tool_result wrapper).
                continue
        if t != "assistant":
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, str):
            collected.append(content)
            continue
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            text = "\n".join(p for p in parts if p)
            if text:
                collected.append(text)

    if not collected:
        return None
    # collected is reverse-chrono; flip back to forward order
    return "\n\n".join(reversed(collected))


def extract_session_label(transcript_path):
    """Most recent customTitle (set by /rename) or auto-generated slug."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if '"customTitle"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ct = obj.get("customTitle")
            if ct:
                return ct
        for line in reversed(lines):
            line = line.strip()
            if '"slug"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            slug = obj.get("slug")
            if slug:
                return slug
    except Exception:
        pass
    return None


def extract_user_prompt(payload):
    for key in ("prompt", "user_message", "message", "content"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return None


def chunk_body(text, header_html, limit=PER_MESSAGE_LIMIT):
    """Split body so each rendered message (header + body) fits under limit.
    Tries paragraph/line/space boundaries before hard-cutting."""
    body_budget = limit - len(header_html) - 30  # room for (i/N) suffix
    if body_budget < 100:
        body_budget = 100  # pathological header — at least give body something
    if len(text) <= body_budget:
        return [text]
    chunks, remaining = [], text
    while remaining:
        if len(remaining) <= body_budget:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, body_budget)
        if cut < body_budget // 2:
            cut = remaining.rfind("\n", 0, body_budget)
        if cut < body_budget // 2:
            cut = remaining.rfind(" ", 0, body_budget)
        if cut < body_budget // 2:
            cut = body_budget
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


def is_pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_worker_running():
    """Spawn the worker as a detached background process if not alive."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if is_pid_alive(pid):
                return  # already running
        except Exception:
            pass
    try:
        if sys.platform == "win32":
            DETACHED = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                [WIN_PYTHON, str(WORKER_SCRIPT)],
                creationflags=DETACHED | CREATE_NO_WINDOW,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                [PYTHON, str(WORKER_SCRIPT)],
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        log("spawned worker")
    except Exception as e:
        log(f"worker spawn failed: {e}")


def ensure_bot_running():
    """Heartbeat: if the Telegram bot is dead, respawn it detached so it can
    keep polling Telegram and writing to the inbox. Note: a detached bot
    can't push MCP notifications to a live session — but its inbox writes
    are picked up on the next SessionStart, so no messages are lost.
    """
    bot_pid_file = Path.home() / ".claude" / "channels" / "telegram" / "bot.pid"
    plugin_dir = Path.home() / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "telegram"
    env_file = Path.home() / ".claude" / "channels" / "telegram" / ".env"

    if bot_pid_file.exists():
        try:
            pid = int(bot_pid_file.read_text().strip())
            if is_pid_alive(pid):
                return  # bot alive, all good
        except Exception:
            pass

    # Bot is dead. Find the plugin dir (hashed subdirectory).
    if not plugin_dir.exists():
        log("bot heartbeat: plugin dir missing, can't revive")
        return
    candidates = [d for d in plugin_dir.iterdir() if d.is_dir()]
    if not candidates:
        log("bot heartbeat: no plugin install found")
        return
    plugin_root = str(candidates[0])

    # Pull token from .env so we don't have to assume it's in the environment.
    token = None
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    if not token:
        log("bot heartbeat: no token in .env, can't revive")
        return

    try:
        env = {**os.environ, "TELEGRAM_BOT_TOKEN": token}
        if sys.platform == "win32":
            DETACHED = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["bun", "--cwd", plugin_root, "server.ts"],
                creationflags=DETACHED | CREATE_NO_WINDOW,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        else:
            subprocess.Popen(
                ["bun", "--cwd", plugin_root, "server.ts"],
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        log("bot heartbeat: respawned detached bot")
    except Exception as e:
        log(f"bot heartbeat: respawn failed: {e}")


def enqueue(text, parse_mode="HTML"):
    """Persist one message to the queue dir as a JSON file."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    # Filename uses timestamp + counter so sort order = enqueue order.
    ts = time.time()
    fname = f"{ts:.6f}-{os.getpid()}.json"
    path = QUEUE_DIR / fname
    payload = {"text": text, "parse_mode": parse_mode, "enqueued_at": ts}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path.name


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        log(f"bad stdin json: {e}")
        return

    event = (
        payload.get("hook_event_name")
        or (sys.argv[1] if len(sys.argv) > 1 else None)
    )

    transcript = payload.get("transcript_path")
    cwd = payload.get("cwd") or ""
    project = os.path.basename(cwd.rstrip("/\\")) if cwd else "?"
    session_label = extract_session_label(transcript)
    if not session_label:
        sid = payload.get("session_id") or ""
        session_label = sid[:6] if sid else "?"
    label = f"{project}/{session_label}"
    ts = datetime.now().strftime("%H:%M")

    if event == "UserPromptSubmit":
        text = extract_user_prompt(payload)
        emoji = "👤"
        is_user = True
    else:
        text = extract_last_assistant_text(transcript)
        emoji = "🤖"
        is_user = False

    if not text:
        log(f"no text (event={event})")
        return

    # Header always shown at top of each chunk so user can identify sender
    # and timestamp without scrolling. Chunk number added when split.
    header_base = f"{emoji} <b>[{ts}] {html_escape(label)}</b>"

    chunks = chunk_body(text, header_base)
    n = len(chunks)

    enqueued = []
    for i, chunk in enumerate(chunks, 1):
        suffix = f" ({i}/{n})" if n > 1 else ""
        header = f"{emoji} <b>[{ts}] {html_escape(label)}{suffix}</b>"
        body_html = html_escape(chunk)
        if is_user:
            message = f"{header}\n<blockquote>{body_html}</blockquote>"
        else:
            message = f"{header}\n{body_html}"
        # Enforce hard cap as last-resort safety
        message = message[:4000]
        try:
            fname = enqueue(message, parse_mode="HTML")
            enqueued.append(fname)
            # tiny stagger so chunks deliver in order even on fast filesystems
            time.sleep(0.001)
        except Exception as e:
            log(f"enqueue failed chunk {i}/{n}: {e}")

    log(f"event={event} chars={len(text)} chunks={n} enqueued={len(enqueued)}")
    # Spawning the worker is enough — the worker now does its own bot
    # heartbeat on a 3-minute timer. We don't block the hook on a bot check.
    ensure_worker_running()


if __name__ == "__main__":
    main()
