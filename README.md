# claude2bot

Mirror every Claude Code conversation to a private Telegram chat. Read your assistant's responses on your phone while Claude works in the background; reply from anywhere.

## What's in here

| Path | Purpose |
|------|---------|
| `hooks/telegram_mirror.py` | Stop / UserPromptSubmit hook. Fast: builds the message, chunks it, enqueues to disk, spawns the worker, exits in ~50 ms. |
| `hooks/telegram_worker.py` | Background drain worker (singleton). Reads queue dir, POSTs to Telegram with retry/backoff and HTML→plain fallback. Idles 2 min then exits; next message wakes a fresh worker. |
| `plugin/server.patched.ts` | Patched upstream Telegram-plugin MCP server. Fixes Windows-only crash on startup (stdin race); see `plugin/PATCHES.md`. |
| `plugin/mcp.example.json` | Patched `.mcp.json` template. Skips the slow `bun install` step that races with Claude Code's MCP attach timeout. |
| `settings.example.json` | Hook block to merge into `~/.claude/settings.json`. |
| `install.ps1` | Windows install script. |
| `skills/install-claude2bot/SKILL.md` | Slash-command-driven installer for non-technical setup. |

## Architecture

```
                                ┌─────────────────────────────┐
   [you in Telegram] ─────►     │ Telegram Bot API (long poll)│
                                └────────────┬────────────────┘
                                             ▼
                              ┌──────────────────────────────┐
                              │  upstream Telegram plugin    │
                              │  (server.patched.ts, MCP)    │
                              └──────────────┬───────────────┘
                                             ▼   inbound MCP notification
                                  ┌─────────────────────────┐
                                  │   Claude Code session   │
                                  └────┬────────────────────┘
              hook fires on every     ▼
              UserPromptSubmit / Stop
                                  ┌──────────────────────────────────┐
                                  │  hooks/telegram_mirror.py        │
                                  │  - extract last assistant text   │
                                  │  - resolve session label         │
                                  │  - chunk if > 3500 chars         │
                                  │  - write JSON to queue/          │
                                  │  - spawn worker if not running   │
                                  └────────────────┬─────────────────┘
                                                   ▼ JSON files
                              ┌──────────────────────────────────────┐
                              │  ~/.claude/hooks/telegram_queue/     │
                              └────────────────┬─────────────────────┘
                                               ▼ poll
                              ┌──────────────────────────────────────┐
                              │  hooks/telegram_worker.py            │
                              │  - singleton (atomic O_EXCL)         │
                              │  - retry/backoff                     │
                              │  - HTML→plain fallback               │
                              └────────────────┬─────────────────────┘
                                               ▼ POST sendMessage
                              ┌──────────────────────────────────────┐
                              │  Telegram Bot API                    │
                              └──────────────────────────────────────┘
```

**Outbound is decoupled.** The hook never blocks on the network. Worker survives session restarts because the queue is on disk. Messages are guaranteed delivered or stay in the queue forever.

**Inbound** uses the upstream `claude-plugins-official/telegram` plugin (with the patches in `plugin/`). Only one Claude Code session at a time can hold the bot's polling lock.

## Requirements

- Claude Code 2.x
- Python 3.11+ on PATH (the install script defaults to `C:\Python314\python.exe`; edit if yours differs)
- Bun (used by the upstream Telegram plugin)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user id (`@userinfobot` will tell you)

## Install (Windows / Git Bash)

```bash
git clone https://github.com/apartsin/claude2bot.git
cd claude2bot
powershell -ExecutionPolicy Bypass -File install.ps1 -Token <BOT_TOKEN> -ChatId <YOUR_USER_ID>
```

The script:
1. Installs the upstream Telegram plugin (if missing) and runs `bun install` in its dir
2. Patches `server.ts` with the Windows fix
3. Writes `~/.claude/channels/telegram/.env` with your token (mode 600)
4. Patches `~/.claude/mcp.json` with the fast launch config
5. Copies `hooks/*.py` to `~/.claude/hooks/`
6. Sets your `chat_id` in `telegram_mirror.py`
7. Merges the hook block into `~/.claude/settings.json`
8. Reminds you to restart Claude Code

After restart:
1. DM your bot, send any message → bot replies with a 6-char pairing code
2. In Claude Code: `/telegram:access pair <code>`
3. (Optional) `/telegram:access policy allowlist`

From here on, every Claude turn mirrors to your Telegram chat with `👤` for your prompts and `🤖` for assistant responses, timestamped, project/session-tagged, chunked if long.

## Install (skill-driven)

If you'd rather have Claude run the install for you:

```
/install-claude2bot
```

The skill (in `skills/install-claude2bot/`) walks through it interactively, asking for token + user id and patching the right files.

## Manual install / other platforms

See `install.ps1` for the exact steps. The hooks themselves are pure Python (stdlib only) and run on macOS/Linux unchanged. The plugin patch is Windows-specific — on macOS/Linux the upstream plugin works without modification.

## Logs / debugging

| File | Contents |
|------|----------|
| `~/.claude/hooks/telegram_mirror.log` | Hook runs: events fired, chunk counts, errors enqueueing |
| `~/.claude/hooks/telegram_worker.log` | Worker activity: deliveries, retries, exits |
| `~/.claude/hooks/telegram_queue/` | Pending messages (empty at steady state) |

If messages stop arriving:
1. Check worker log — last entry should be recent and say `delivered ... OK`
2. Check queue dir — non-empty means worker isn't draining (probably crashed)
3. Run worker manually: `/c/Python314/python ~/.claude/hooks/telegram_worker.py` and watch
4. Verify token: `curl https://api.telegram.org/bot$TOKEN/getMe`

## License

MIT. See LICENSE.
