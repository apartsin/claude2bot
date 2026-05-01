# Telegram plugin patches

Two issues were fixed in the upstream Telegram plugin (`claude-plugins-official/telegram`) on Windows:

## 1. Server crashed immediately on Claude Code startup

**Root cause:** Claude Code's MCP stdio pipe on Windows fires `stdin.on('end')` and `stdin.on('close')` events spuriously at startup, before the MCP handshake completes. The upstream `server.ts` interpreted those as parent disconnect and exited within ~50 ms.

**Fix in `server.patched.ts`:** Replaced stdin-based lifecycle with parent-PID polling. Records `process.ppid` at boot, polls every 5 s with `process.kill(pid, 0)`. If the parent process is gone, shut down. On non-Windows, the original POSIX behaviour (stdin EOF / ppid reparent) is preserved.

```diff
-process.stdin.on('end', shutdown)
-process.stdin.on('close', shutdown)
+if (process.platform === 'win32') {
+  // Parent-alive check: process.kill(pid, 0) throws ESRCH if pid is gone.
+  setInterval(() => { try { process.kill(bootPpid, 0) } catch { shutdown() } }, 5000).unref()
+} else {
+  process.stdin.on('end', shutdown)
+  process.stdin.on('close', shutdown)
+  // ... existing watchdog
+}
```

## 2. MCP attach race during `bun install`

**Root cause:** Upstream `.mcp.json` invoked `bun run start`, which runs `bun install --no-summary && bun server.ts`. Even with cache hits, the install step delayed the MCP `initialize` response long enough that Claude Code timed out and closed the pipe.

**Fix in `mcp.example.json`:** Skip the `start` script wrapper and invoke `bun server.ts` directly. Dependencies must already be installed (run `bun install` once in the plugin dir).

## Applying

1. Copy `server.patched.ts` over the upstream `server.ts` in your installed plugin dir
2. Replace your `.mcp.json` with `mcp.example.json` (after filling in token + paths)
3. Restart Claude Code
