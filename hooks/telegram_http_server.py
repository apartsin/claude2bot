"""HTTP server that handles Telegram-mirror hooks WITHOUT spawning a shell.

When Claude Code's harness invokes a `type: command` hook on Windows it spawns
bash.exe → pythonw.exe per fire. bash.exe is CONSOLE subsystem and flashes a
window. The harness's `type: http` hook posts JSON to a URL with no spawn at
all — silent.

This server runs as a singleton background daemon (started once by
telegram_mirror.py the next time a hook fires; thereafter every hook is a
plain HTTP POST). On a 200ms-startup-amortised basis, hook latency is the
same as the command hook but with zero process churn.

Endpoints:
  POST /stop                Stop hook payload (full transcript final text)
  POST /user                UserPromptSubmit hook payload (user message)
  POST /stream              PostToolUse hook payload (incremental text only)
  POST /drain               SessionStart drain (returns additionalContext)
  GET  /health              200 OK while server is up
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

HOOKS_DIR = Path.home() / ".claude" / "hooks"
QUEUE_DIR = HOOKS_DIR / "telegram_queue"
INBOX_DIR = HOOKS_DIR / "telegram_inbox"
CURSOR_FILE = HOOKS_DIR / ".last_mirrored_uuid"
PID_FILE = HOOKS_DIR / "telegram_http_server.pid"
LOG_FILE = HOOKS_DIR / "telegram_http_server.log"
PORT = 53117       # arbitrary; nothing else uses it on this box
HOST = "127.0.0.1"
MAX_TOTAL_CHARS = 3500


def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] http[{os.getpid()}] {msg}\n")
    except Exception:
        pass


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ── Singleton via Windows named mutex ──────────────────────────────────────
_SINGLETON_HANDLE = None


def acquire_singleton() -> bool:
    global _SINGLETON_HANDLE
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        name = f"Local\\claude-telegram-http-{os.getlogin()}"
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        h = kernel32.CreateMutexW(None, True, name)
        if not h or kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            if h:
                kernel32.CloseHandle(h)
            return False
        _SINGLETON_HANDLE = h
        try:
            PID_FILE.write_text(str(os.getpid()))
        except Exception:
            pass
        return True
    # POSIX path
    if PID_FILE.exists():
        try:
            other = int(PID_FILE.read_text().strip())
            if is_pid_alive(other) and other != os.getpid():
                return False
        except Exception:
            pass
    PID_FILE.write_text(str(os.getpid()))
    return True


# ── Hook payload handlers ─────────────────────────────────────────────────


def _enqueue(text: str, parse_mode: str = "HTML") -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{time.time():.6f}-http.json"
    payload = {"text": text[:4000], "parse_mode": parse_mode, "enqueued_at": time.time()}
    (QUEUE_DIR / fname).write_text(json.dumps(payload), encoding="utf-8")
    _ensure_worker_running()


_WORKER_PID_FILE = HOOKS_DIR / "telegram_worker.pid"


def _ensure_worker_running() -> None:
    """Spawn the worker daemon if not alive. The worker exits after 2 min idle
    (which is why we re-spawn it whenever something gets enqueued). Cheap:
    one is_pid_alive syscall when worker is alive, full spawn when not."""
    try:
        if _WORKER_PID_FILE.exists():
            try:
                pid = int(_WORKER_PID_FILE.read_text().strip())
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                        return  # alive
                    except OSError:
                        pass
            except Exception:
                pass
        import subprocess
        worker_py = HOOKS_DIR / "telegram_worker.py"
        win_python = r"C:\Python314\pythonw.exe" if sys.platform == "win32" else "/c/Python314/python"
        if sys.platform == "win32":
            DETACHED = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            subprocess.Popen(
                [win_python, str(worker_py)],
                creationflags=DETACHED | CREATE_NO_WINDOW,
                startupinfo=si, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                [win_python, str(worker_py)],
                start_new_session=True, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        log(f"worker spawn failed: {e}")


def _label(payload: dict) -> str:
    cwd = payload.get("cwd") or ""
    project = os.path.basename(cwd.rstrip("/\\")) if cwd else "?"
    transcript = payload.get("transcript_path")
    slug = "?"
    if transcript and os.path.exists(transcript):
        try:
            with open(transcript, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                if '"customTitle"' in line:
                    try:
                        obj = json.loads(line.strip())
                        if obj.get("customTitle"):
                            slug = obj["customTitle"]
                            break
                    except Exception:
                        continue
            else:
                for line in reversed(lines):
                    if '"slug"' in line:
                        try:
                            obj = json.loads(line.strip())
                            if obj.get("slug"):
                                slug = obj["slug"]
                                break
                        except Exception:
                            continue
        except Exception:
            pass
    return f"{project}/{slug}"


def _extract_assistant_blocks(transcript_path: str, since_uuid: str = "",
                              current_turn_only: bool = False) -> tuple[str, str]:
    """Returns (concatenated_text, last_uuid).

    If current_turn_only is True (Stop hook), walk backward from the end and
    collect assistant text blocks until hitting a user/human entry — that
    boundary marks the start of the current turn. This prevents Stop hooks
    from re-emitting the entire transcript history every fire.

    If False (PostToolUse stream), walk forward from cursor `since_uuid` to
    pick up incremental new entries.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return "", since_uuid
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return "", since_uuid

    if current_turn_only:
        # Backward-walk: collect assistant entries until we hit a user entry.
        collected: list[str] = []  # reverse-chrono
        last_uuid = ""
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
                if collected:
                    break
                continue  # skip past tool-result wrappers etc.
            if t != "assistant":
                continue
            if not last_uuid:
                last_uuid = obj.get("uuid", "")
            msg = obj.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text.strip():
                            parts.append(text)
                if parts:
                    collected.append("\n".join(parts))
        if not collected:
            return "", last_uuid
        return "\n\n".join(reversed(collected)), last_uuid

    # Forward-walk from cursor (PostToolUse stream behavior)
    found = (since_uuid == "")
    parts_fwd: list[str] = []
    last_uuid = since_uuid
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
        u = obj.get("uuid", "")
        if not found:
            if u == since_uuid:
                found = True
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    if t.strip():
                        parts_fwd.append(t)
        last_uuid = u
    return "\n\n".join(parts_fwd), last_uuid


def handle_stop(payload: dict) -> None:
    """End-of-turn full assistant text — current turn only."""
    transcript = payload.get("transcript_path")
    text, _ = _extract_assistant_blocks(transcript, current_turn_only=True)
    if not text:
        return
    label = _label(payload)
    ts = datetime.now().strftime("%H:%M")
    header = f"🤖 <b>[{ts}] {html_escape(label)}</b>"
    body = f"{header}\n{html_escape(text)[:MAX_TOTAL_CHARS]}"
    _enqueue(body)


def handle_user(payload: dict) -> None:
    """User prompt mirror."""
    prompt = (payload.get("prompt") or payload.get("user_message") or
              payload.get("message") or payload.get("content") or "")
    if not prompt or not prompt.strip():
        return
    label = _label(payload)
    ts = datetime.now().strftime("%H:%M")
    header = f"👤 <b>[{ts}] {html_escape(label)}</b>"
    body = f"{header}\n<blockquote>{html_escape(prompt)[:MAX_TOTAL_CHARS]}</blockquote>"
    _enqueue(body)


def handle_stream(payload: dict) -> None:
    """Incremental text since last cursor (PostToolUse)."""
    transcript = payload.get("transcript_path")
    cursor = ""
    if CURSOR_FILE.exists():
        try:
            cursor = CURSOR_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    text, new_cursor = _extract_assistant_blocks(transcript, cursor)
    if cursor and new_cursor != cursor:
        try:
            CURSOR_FILE.write_text(new_cursor, encoding="utf-8")
        except Exception:
            pass
    if not text:
        return
    label = _label(payload)
    ts = datetime.now().strftime("%H:%M")
    header = f"🤖 <b>[{ts}] {html_escape(label)}</b> <i>(stream)</i>"
    body = f"{header}\n{html_escape(text)[:MAX_TOTAL_CHARS]}"
    _enqueue(body)


def handle_drain(payload: dict) -> dict:
    """SessionStart / UserPromptSubmit drain. Returns hookSpecificOutput dict."""
    if not INBOX_DIR.exists():
        return {}
    files = sorted(INBOX_DIR.glob("*.json"))
    if not files:
        return {}
    parts = ["[Telegram messages received while no session was reading them]\n"]
    consumed = []
    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            try:
                f.unlink()
            except Exception:
                pass
            continue
        params = obj.get("params", {})
        content = params.get("content", "")
        meta = params.get("meta", {})
        block = (
            f"<channel source=\"telegram\" chat_id=\"{meta.get('chat_id','?')}\" "
            f"message_id=\"{meta.get('message_id','')}\" user=\"{meta.get('user','?')}\" "
            f"ts=\"{meta.get('ts','?')}\">\n{content}\n</channel>\n"
        )
        parts.append(block)
        consumed.append(f)
        if sum(len(p) for p in parts) > 8000:
            parts.append(f"\n[...{len(files)-len(consumed)} more remain]\n")
            break
    for f in consumed:
        try:
            f.unlink()
        except Exception:
            pass
    if len(parts) <= 1:
        return {}
    digest = "\n".join(parts)
    event = payload.get("hook_event_name", "SessionStart")
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": digest}}


# ── HTTP plumbing ─────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:
            log(f"bad request: {e}")
            self.send_error(400)
            return

        try:
            if self.path == "/stop":
                handle_stop(payload)
                self._respond_json({})
            elif self.path == "/user":
                handle_user(payload)
                self._respond_json({})
            elif self.path == "/stream":
                handle_stream(payload)
                self._respond_json({})
            elif self.path == "/drain":
                out = handle_drain(payload)
                self._respond_json(out)
            else:
                self.send_error(404)
        except Exception as e:
            log(f"handler error {self.path}: {e}")
            self.send_error(500)

    def _respond_json(self, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # Suppress default per-request stderr log; we have our own log file.
        pass


def main() -> None:
    if not acquire_singleton():
        sys.exit(0)
    log(f"starting on {HOST}:{PORT}")
    httpd = HTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except Exception:
            pass
        log("stopped")


if __name__ == "__main__":
    main()
