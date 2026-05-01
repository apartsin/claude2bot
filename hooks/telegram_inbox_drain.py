"""SessionStart hook: drain ~/.claude/hooks/telegram_inbox/ into the new
session's context.

The Telegram-plugin bot writes one JSON file per inbound message into the
inbox dir, in parallel with its real-time MCP notification. If a session
crashes/restarts or no session was alive when a message arrived, the MCP
push gets lost — but the file persists. This hook reads any files there at
session start, formats them as a digest, and emits them via
`hookSpecificOutput.additionalContext` so the model sees them on turn 1.

Files are deleted after reading. Empty inbox = silent no-op.

Output JSON shape:
    {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "[Telegram messages received while you were offline]\n..."
    }}
"""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

INBOX_DIR = Path.home() / ".claude" / "hooks" / "telegram_inbox"
LOG_FILE = Path.home() / ".claude" / "hooks" / "telegram_inbox_drain.log"
MAX_DIGEST_CHARS = 8000  # cap injected context to avoid bloating the turn


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def main():
    # Read stdin to consume the hook payload (we don't need anything from it,
    # but we must read so the parent doesn't hang on its end of the pipe).
    try:
        sys.stdin.read()
    except Exception:
        pass

    if not INBOX_DIR.exists():
        # Nothing to drain. Emit empty JSON (harness treats missing
        # hookSpecificOutput as "no additional context").
        print("{}")
        return

    files = sorted(INBOX_DIR.glob("*.json"))
    if not files:
        print("{}")
        return

    parts = ["[Telegram messages received while no session was reading them]\n"]
    consumed = []
    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"corrupt {f.name}: {e}; deleting")
            try:
                f.unlink()
            except Exception:
                pass
            continue
        params = obj.get("params", {})
        content = params.get("content", "")
        meta = params.get("meta", {})
        ts = meta.get("ts", "?")
        user = meta.get("user", "?")
        chat_id = meta.get("chat_id", "?")
        msg_id = meta.get("message_id", "")

        block = (
            f"<channel source=\"telegram\" chat_id=\"{chat_id}\" "
            f"message_id=\"{msg_id}\" user=\"{user}\" ts=\"{ts}\">\n"
            f"{content}\n"
            f"</channel>\n"
        )
        parts.append(block)
        consumed.append(f)
        # Stop adding more if we'd blow the budget; remaining files stay
        # in inbox and will be picked up next session start.
        if sum(len(p) for p in parts) > MAX_DIGEST_CHARS:
            parts.append(f"\n[...{len(files) - len(consumed)} more messages remain in inbox]\n")
            break

    # Delete consumed files only AFTER constructing the digest, so a crash
    # mid-format leaves the inbox intact and a future drain can retry.
    for f in consumed:
        try:
            f.unlink()
        except Exception as e:
            log(f"unlink failed {f.name}: {e}")

    digest = "\n".join(parts)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": digest,
        }
    }
    print(json.dumps(out))
    log(f"drained {len(consumed)}/{len(files)} files, digest {len(digest)} chars")


if __name__ == "__main__":
    main()
