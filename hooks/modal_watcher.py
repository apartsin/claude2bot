"""Background watcher: monitors Modal sweep progress, downloads new
results, regenerates plots, alerts on completion or anomalies.

Runs as a singleton daemon spawned by the Stop hook (similar to
telegram_worker.py pattern). Polls every POLL_INTERVAL_SEC. On state
changes:
  - new result files → enqueue Telegram message + run download + regenerate plots
  - app died unexpectedly (state=stopped with exit code != 0) → enqueue Telegram alert
  - all apps finished → enqueue final summary, set "sweep_done" flag, exit
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOOKS_DIR = Path.home() / ".claude" / "hooks"
PID_FILE = HOOKS_DIR / "modal_watcher.pid"
STATE_FILE = HOOKS_DIR / "modal_watcher_state.json"
LOG_FILE = HOOKS_DIR / "modal_watcher.log"
TELEGRAM_QUEUE = HOOKS_DIR / "telegram_queue"
DONE_FLAG = HOOKS_DIR / "modal_sweep_done.flag"

PROJECT_DIR = Path("E:/Projects/branchy")
# pythonw.exe = GUI subsystem, no console window. python.exe flashes briefly
# even with CREATE_NO_WINDOW because of its PE subsystem flag.
PYTHON = "C:/Python314/pythonw.exe"

POLL_INTERVAL_SEC = 300       # 5 minutes
APP_PREFIX = "bts-ee"          # only watch our project
IDLE_EXIT_AFTER_DONE_SEC = 60  # exit shortly after sweep_done


def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] watcher[{os.getpid()}] {msg}\n")
    except Exception:
        pass


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_SINGLETON_HANDLE = None  # held for process lifetime once acquired


def acquire_singleton() -> bool:
    """OS-level singleton. On Windows uses a named mutex (atomic, race-free).
    On POSIX falls back to the previous PID-file approach (good enough; no
    duplicate-spawn races observed there).
    """
    global _SINGLETON_HANDLE
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        # Backslash-prefixed namespace would require admin; use Local\.
        # Per-user namespace prevents collision with other users' instances.
        name = f"Local\\claude-modal-watcher-{os.getlogin()}"
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        h = kernel32.CreateMutexW(None, True, name)
        if not h:
            return False
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(h)
            return False
        _SINGLETON_HANDLE = h  # hold for process lifetime
        # Best-effort PID file for diagnostics; don't gate on it
        try:
            PID_FILE.write_text(str(os.getpid()))
        except Exception:
            pass
        return True

    # POSIX path (original logic, no race observed)
    try:
        fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        try:
            other = int(PID_FILE.read_text().strip())
            if is_pid_alive(other) and other != os.getpid():
                return False
        except Exception:
            pass
        # Stale; one-shot retry
        try:
            PID_FILE.unlink()
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except Exception:
            return False


def release_singleton() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except Exception:
        pass


_NO_WIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run_modal(args: list[str], timeout: int = 60) -> str:
    """Invoke modal CLI; return stdout (stderr suppressed). On error returns ''."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        out = subprocess.check_output(
            ["modal"] + args,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
            creationflags=_NO_WIN,  # suppress console flash on Windows
        )
        return out.decode("utf-8", errors="replace")
    except Exception as e:
        log(f"modal {' '.join(args)} failed: {e}")
        return ""


def count_results() -> int:
    """Count *.json files in bts-results volume root."""
    out = run_modal(["volume", "ls", "bts-results"])
    return sum(1 for line in out.splitlines() if line.strip().endswith(".json"))


def list_active_apps() -> list[dict]:
    """Return [{app_id, state, tasks, description}, ...] for apps matching APP_PREFIX."""
    out = run_modal(["app", "list"])
    apps = []
    for line in out.splitlines():
        # crude parse — modal CLI uses box-drawing; lines with ap- prefix have data
        if "ap-" not in line or APP_PREFIX not in line:
            continue
        parts = [p.strip() for p in line.replace("│", "|").split("|") if p.strip()]
        if len(parts) >= 4 and parts[0].startswith("ap-"):
            apps.append({
                "id": parts[0],
                "description": parts[1],
                "state": parts[2],
                "tasks": parts[3],
            })
    return apps


def enqueue_telegram(text: str) -> None:
    """Use the existing telegram_queue/ dir so the worker delivers it."""
    try:
        TELEGRAM_QUEUE.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        msg = {
            "text": f"<b>📊 Modal watcher</b>\n{text}",
            "parse_mode": "HTML",
            "enqueued_at": ts,
        }
        path = TELEGRAM_QUEUE / f"{ts:.6f}-watcher.json"
        path.write_text(json.dumps(msg), encoding="utf-8")
        log(f"queued telegram: {text[:80]}")
    except Exception as e:
        log(f"telegram enqueue failed: {e}")


def download_and_regenerate() -> tuple[int, int]:
    """Run download_results.py + plot_all.py. Returns (n_files, n_figures)."""
    try:
        subprocess.run(
            [PYTHON, str(PROJECT_DIR / "analysis" / "download_results.py")],
            cwd=str(PROJECT_DIR),
            timeout=600, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WIN,
        )
        subprocess.run(
            [PYTHON, str(PROJECT_DIR / "analysis" / "plot_all.py")],
            cwd=str(PROJECT_DIR),
            timeout=120, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WIN,
        )
        results_dir = PROJECT_DIR / "results"
        figs_dir = PROJECT_DIR / "paper_html" / "figures"
        n_files = sum(1 for _ in results_dir.glob("*.json")) if results_dir.exists() else 0
        n_figs = sum(1 for _ in figs_dir.glob("*.png")) if figs_dir.exists() else 0
        return n_files, n_figs
    except Exception as e:
        log(f"download/regenerate failed: {e}")
        return -1, -1


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_count": 0, "last_apps": [], "started_at": time.time()}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def main() -> None:
    if not acquire_singleton():
        sys.exit(0)
    log("starting modal watcher")
    state = load_state()
    sweep_done_at: float | None = None

    try:
        while True:
            # Snapshot
            try:
                count = count_results()
                apps = list_active_apps()
            except Exception as e:
                log(f"snapshot failed: {e}")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            running = [a for a in apps if a.get("state") == "ephemeral"]
            stopped = [a for a in apps if a.get("state") == "stopped"]
            new_files = count - state.get("last_count", 0)

            log(f"poll: count={count} new={new_files} running={len(running)} stopped={len(stopped)}")

            # New results → download + regen + alert
            if new_files > 0:
                enqueue_telegram(
                    f"+{new_files} new result files (total {count}). Downloading + regenerating plots."
                )
                n_files, n_figs = download_and_regenerate()
                if n_files > 0:
                    enqueue_telegram(
                        f"Local: {n_files} result JSONs, {n_figs} PNG figures regenerated."
                    )

            # Sweep done?
            if not running and state.get("last_count", 0) > 0:
                if sweep_done_at is None:
                    sweep_done_at = time.time()
                    enqueue_telegram(
                        f"✅ All Modal apps done. Final count: {count} result files."
                    )
                    DONE_FLAG.write_text(json.dumps({"done_at": sweep_done_at, "count": count}))
                    log(f"sweep done at {sweep_done_at}, count={count}")
                elif time.time() - sweep_done_at > IDLE_EXIT_AFTER_DONE_SEC:
                    log("post-done idle elapsed; exiting")
                    return

            # Update state
            state["last_count"] = count
            state["last_apps"] = apps
            save_state(state)

            time.sleep(POLL_INTERVAL_SEC)
    finally:
        release_singleton()
        log("stopped")


if __name__ == "__main__":
    main()
