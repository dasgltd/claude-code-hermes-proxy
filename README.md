# claude-proxy

A local proxy that translates [Anthropic Messages API](https://docs.anthropic.com/en/api/messages) calls into `claude` CLI subprocess calls.

## Why

Third-party apps (Hermes, Open WebUI, custom scripts) that use a **Claude Code OAuth token** to call `api.anthropic.com` get:

```
HTTP 400: Third-party apps now draw from your extra usage, not your plan limits.
```

This happens because Anthropic distinguishes between first-party apps (Claude Code, Claude Desktop) and third-party API callers. Only first-party apps consume from your Pro/Max plan — third-party apps require separate "extra usage" credits.

**claude-proxy** fixes this by routing calls through the official `claude` CLI binary, which is a first-party Anthropic app. The proxy:

1. Accepts any `POST /v1/messages` request (Anthropic Messages API format)
2. Translates it into a `claude -p "..."` subprocess call
3. Streams the response back in Anthropic SSE format

Your plan limits apply, exactly as when you use Claude Code directly.

## Requirements

- **Claude Code CLI** installed and authenticated:
  ```bash
  npm install -g @anthropic-ai/claude-code
  claude   # authenticate
  ```
- **Python 3.10+** with `venv` support
- **Linux** with systemd (macOS support: see below)

## Install

```bash
git clone https://github.com/dasgltd/claude-code-hermes-proxy
cd claude-proxy
bash install.sh
```

The script:
- Copies `proxy.py` to `~/.local/lib/claude-proxy/`
- Creates a Python venv and installs `fastapi` + `uvicorn`
- Installs and starts a systemd user service on port `11435`

Custom port:
```bash
PROXY_PORT=8080 bash install.sh
```

## Configure your app

Point any Anthropic client at the proxy:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:11435
export ANTHROPIC_API_KEY=placeholder   # any non-empty string; ignored by proxy
```

The `ANTHROPIC_API_KEY` is required by most SDKs for format validation but is never sent anywhere — the proxy ignores it and the `claude` CLI handles auth from `~/.claude/.credentials.json`.

## Hermes integration

In `~/.hermes/config.yaml`:
```yaml
model:
  provider: anthropic
  default: claude-sonnet-4-6   # or claude-opus-4-8, claude-haiku-4-5
```

In `~/.hermes/.env`:
```env
ANTHROPIC_BASE_URL=http://127.0.0.1:11435
ANTHROPIC_API_KEY=placeholder
ANTHROPIC_TOKEN=placeholder
```

Then restart the gateway:
```bash
systemctl --user restart hermes-gateway
```

## Verify

```bash
# Health check
curl http://127.0.0.1:11435/health

# Test non-streaming
curl -s -X POST http://127.0.0.1:11435/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say ok"}]}'

# Test streaming
curl -s -X POST http://127.0.0.1:11435/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say ok"}],"stream":true}'
```

## Model selection

The model is passed through from the request. Set the default in your app's config.

| Model | Notes |
|-------|-------|
| `claude-haiku-4-5` | Fastest |
| `claude-sonnet-4-6` | Recommended balance |
| `claude-opus-4-8` | Most capable (Pro has usage limits) |

## Manage the service

```bash
systemctl --user status claude-proxy
systemctl --user restart claude-proxy
systemctl --user stop claude-proxy
journalctl --user -u claude-proxy -f   # live logs
```

## macOS

macOS uses launchd instead of systemd. After install, create a plist manually:

```xml
<!-- ~/Library/LaunchAgents/com.claude-proxy.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude-proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/lib/claude-proxy/venv/bin/python</string>
    <string>/Users/YOU/.local/lib/claude-proxy/proxy.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>/Users/YOU</string>
    <key>PATH</key><string>/Users/YOU/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.claude-proxy.plist
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_BIN` | auto-detected from PATH | Path to `claude` binary |
| `PROXY_HOST` | `127.0.0.1` | Bind address |
| `PROXY_PORT` | `11435` | Bind port |

## How it works

```
App (Hermes, etc.)
  │  POST /v1/messages  {"messages":[...], "stream":true}
  ▼
claude-proxy (127.0.0.1:11435)
  │  claude -p "..." --output-format stream-json --model claude-sonnet-4-6
  ▼
claude CLI  (~/.claude/.credentials.json → OAuth token)
  │  POST api.anthropic.com/v1/messages  [identified as Claude Code]
  ▼
Anthropic API  →  plan limits apply  ✓
```

Multi-turn conversations: prior messages are formatted as `<conversation_history>` and prepended to the system prompt. The last user message becomes the CLI prompt.

## Uninstall

```bash
systemctl --user disable --now claude-proxy
rm -rf ~/.local/lib/claude-proxy
rm ~/.config/systemd/user/claude-proxy.service
systemctl --user daemon-reload
```
