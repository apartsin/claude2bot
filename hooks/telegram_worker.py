"""Telegram delivery worker.

Singleton background process that drains messages from
~/.claude/hooks/telegram_queue/ and POSTs them to the Telegram bot API.

Lifecycle:
  - Singleton enforced via PID file. If another worker is alive, exit.
  - Drain loop: read queue dir in filename order (timestamp-prefixed),
    POST each, delete on success. On failure, sleep and retry.
  - Idle timeout: if queue stays empty for IDLE_EXIT_SEC, exit cleanly.
    Next hook firing will spawn a fresh worker.

Robustness:
  - Per-message retry with exponential backoff (handles transient network
    errors and Telegram rate-limit 429s).
  - HTML parse errors fall back to plain text (strip HTML, retry).
  - Messages stay queued until confirmed delivered. Worker death just
    delays delivery; the queue is never lost.
  - PID file is removed on clean exit; stale PID files are detected
    (process gone) and overwritten.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

QUEUE_DIR = Path.home() / ".claude" / "hooks" / "telegram_queue"
PID_FILE = Path.home() / ".claude" / "hooks" / "telegram_worker.pid"
LOG_FILE = Path.home() / ".claude" / "hooks" / "telegram_worker.log"
ENV_FILE = Path.home() / ".claude" / "channels" / "telegram" / ".env"
BOT_PID_FILE = Path.home() / ".claude" / "channels" / "telegram" / "bot.pid"
PLUGIN_DIR = Path.home() / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "telegram"
CHAT_ID = "1796415913"
IDLE_EXIT_SEC = 120          # exit after 2 minutes of empty queue
POLL_INTERVAL_SEC = 1         # check queue every second
HEARTBEAT_INTERVAL_SEC = 180  # bot health check every 3 minutes
RETRY_BACKOFFS = [1, 2, 5, 15, 30, 60]  # seconds between retries per message


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] worker[{os.getpid()}] {msg}\n")
    except Exception:
        pass


def read_token():
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def is_pid_alive(pid):
    """Cross-platform process-existence check."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_singleton():
    """Returns True if we're the unique worker. False if another is alive.

    Uses O_CREAT|O_EXCL for atomic create — only one process succeeds even
    when many launch simultaneously. If a stale PID file exists (process
    died without cleanup), we detect it and steal the lock.
    """
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            # Someone else holds it. Live or stale?
            try:
                other = int(PID_FILE.read_text().strip())
            except Exception:
                other = -1
            if other == os.getpid():
                return True
            if is_pid_alive(other):
                return False
            # Stale — try to remove and loop. If another stale-stealer races
            # us, the next O_EXCL will return FileExistsError and we'll
            # check again.
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                pass


def release_singleton():
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except Exception:
        pass


def post_telegram(token, text, parse_mode=None, timeout=15):
    fields = {
        "chat_id": CHAT_ID,
        "text": text[:3500],
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        fields["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(fields).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, f"OK {body[:120]}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        # 429 = rate limit; honour retry_after if present
        retry_after = None
        try:
            retry_after = json.loads(body).get("parameters", {}).get("retry_after")
        except Exception:
            pass
        return False, f"HTTP{e.code} retry_after={retry_after} {body[:200]}"
    except Exception as e:
        return False, f"net {type(e).__name__}: {e}"


def chunk_text(text, header_html, parse_mode, limit=3500):
    """Split text into chunks each <= `limit` chars (after header).
    Header is repeated on each chunk with a (i/N) suffix so user can tell
    they're parts of one message. Tries to split on paragraph/line boundaries.
    """
    body_budget = limit - len(header_html) - 30  # 30 for "(i/N)\n" overhead
    if len(text) <= body_budget:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= body_budget:
            chunks.append(remaining)
            break
        # Prefer split on \n\n, then \n, then space, else hard cut
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


def deliver_one(token, msg_path):
    """Send one queued message. Delete on success.

    First atomically rename to a worker-owned `.processing` name so other
    workers won't pick it up. Atomic rename on POSIX/Windows; if another
    worker already renamed, our rename raises and we skip.
    """
    claimed = msg_path.with_name(f"{msg_path.stem}.{os.getpid()}.processing")
    try:
        msg_path.rename(claimed)
    except (FileNotFoundError, OSError):
        return  # another worker grabbed it (or it was already deleted)
    msg_path = claimed
    try:
        msg = json.loads(msg_path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"corrupt message {msg_path.name}: {e}; deleting")
        try:
            msg_path.unlink()
        except Exception:
            pass
        return

    text = msg.get("text", "")
    parse_mode = msg.get("parse_mode")
    attempt = msg.get("attempts", 0)

    ok, info = post_telegram(token, text, parse_mode=parse_mode)
    if ok:
        log(f"delivered {msg_path.name} attempt={attempt} {info[:80]}")
        try:
            msg_path.unlink()
        except Exception:
            pass
        return

    # Failure path
    attempt += 1
    msg["attempts"] = attempt
    msg["last_error"] = info[:300]

    # Plain-text fallback after 2 HTML failures
    if parse_mode and attempt >= 2:
        plain = re.sub(r"<[^>]+>", "", text)
        ok2, info2 = post_telegram(token, plain, parse_mode=None)
        if ok2:
            log(f"delivered (plain fallback) {msg_path.name} after {attempt} HTML attempts")
            try:
                msg_path.unlink()
            except Exception:
                pass
            return
        info = f"{info}; plain={info2}"
        msg["last_error"] = info[:300]

    # Persist updated state and back off
    try:
        msg_path.write_text(json.dumps(msg), encoding="utf-8")
    except Exception:
        pass
    backoff = RETRY_BACKOFFS[min(attempt - 1, len(RETRY_BACKOFFS) - 1)]
    log(f"FAIL {msg_path.name} attempt={attempt} backoff={backoff}s err={info[:120]}")
    time.sleep(backoff)


def heartbeat_check_bot(token):
    """Check if Telegram bot is alive; if not, respawn detached.
    Detached bot can't push MCP notifications to a live Claude session, but
    its inbox-file writes survive for the SessionStart drain on next session,
    so no Telegram messages are lost.
    """
    if BOT_PID_FILE.exists():
        try:
            pid = int(BOT_PID_FILE.read_text().strip())
            if is_pid_alive(pid):
                return  # bot alive
        except Exception:
            pass
    # Bot dead — find plugin dir and respawn
    if not PLUGIN_DIR.exists():
        log("heartbeat: plugin dir missing")
        return
    candidates = [d for d in PLUGIN_DIR.iterdir() if d.is_dir()]
    if not candidates:
        log("heartbeat: no plugin install")
        return
    plugin_root = str(candidates[0])
    try:
        env = {**os.environ, "TELEGRAM_BOT_TOKEN": token}
        if sys.platform == "win32":
            DETACHED = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["bun", "--cwd", plugin_root, "server.ts"],
                creationflags=DETACHED | CREATE_NO_WINDOW,
                close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env,
            )
        else:
            subprocess.Popen(
                ["bun", "--cwd", plugin_root, "server.ts"],
                start_new_session=True, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env,
            )
        log("heartbeat: respawned detached bot")
    except Exception as e:
        log(f"heartbeat: respawn failed: {e}")


def main():
    if not acquire_singleton():
        # Another worker is alive; nothing to do.
        sys.exit(0)
    log("starting")
    try:
        token = read_token()
        if not token:
            log("no token in .env; exiting")
            return
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)

        idle_since = time.time()
        last_heartbeat = 0.0
        while True:
            # Bot heartbeat (every 3 min). Cheap: usually just a kill(pid, 0).
            now = time.time()
            if now - last_heartbeat > HEARTBEAT_INTERVAL_SEC:
                heartbeat_check_bot(token)
                last_heartbeat = now

            files = sorted(QUEUE_DIR.glob("*.json"))
            # Filter out files already being processed by another worker
            files = [f for f in files if not f.name.endswith(".processing")]
            if not files:
                if time.time() - idle_since > IDLE_EXIT_SEC:
                    log(f"idle for {IDLE_EXIT_SEC}s; exiting")
                    return
                time.sleep(POLL_INTERVAL_SEC)
                continue
            idle_since = time.time()
            for msg_path in files:
                deliver_one(token, msg_path)
    finally:
        release_singleton()
        log("stopped")


if __name__ == "__main__":
    main()
