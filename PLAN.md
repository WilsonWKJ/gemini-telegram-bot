# Gemini Telegram Bot — Design Plan

## Problem Statement

Currently, operating the Lift infrastructure requires:
1. Mac with VPN → SSH to kube-controller → VSCode → Cline ACT mode
2. This chain is **not available on mobile** — can't debug from phone when away from laptop

## Solution

A Telegram bot running on kube-controller that:
1. Accepts messages from Telegram (polling mode, outbound-only)
2. Routes them to Claude AI via TrendMicro's internal AI Gateway
3. Executes infrastructure commands on kube-controller
4. Returns results to Telegram

## Network Flow

```
                    OUTBOUND ONLY (no ports exposed)
                    ┌──────────────────────────────┐
📱 Telegram ──→ ☁️ ──→│ kube-controller              │
                    │  Bot polls api.telegram.org   │
                    │  AI calls api.rdsec.tm.com    │
                    │  kubectl / ssh / govc local   │
                    └──────────────────────────────┘
```

All connections are **outbound HTTPS from kube-controller**:
- `api.telegram.org` — Telegram Bot API (polling + sending messages)
- `api.rdsec.trendmicro.com` — AI inference (Anthropic-compatible)
- Local commands — kubectl, ssh, govc (no network needed)

## API Endpoint Analysis

### Gemini API via Google AI
- **URL**: `https://api.rdsec.trendmicro.com/prod/aiendpoint/`
- **Protocol**: Anthropic Messages API compatible
- **Auth**: JWT Bearer token (`ANTHROPIC_AUTH_TOKEN`)
- **Models available**: `claude-4.6-sonnet-aws[1m]`, `claude-4.6-opus-aws[1m]`, `claude-4.5-haiku`
- **Token expiry**: 2026-06-15 (needs renewal every ~3 months)
- **Cost**: Free (company-provided)

### How to call it
```python
import httpx

response = httpx.post(
    "https://api.rdsec.trendmicro.com/prod/aiendpoint/v1/messages",
    headers={
        "x-api-key": ANTHROPIC_AUTH_TOKEN,  # JWT token
        "anthropic-version": "2024-11-01",
        "content-type": "application/json",
    },
    json={
        "model": "claude-4.6-sonnet-aws[1m]",
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": conversation_history,
    },
    timeout=120.0,
)
```

## Component Design

### 1. Main Entry Point (`main.py`)
- Load config from secrets file
- Initialize Telegram bot (polling mode)
- Register command handlers
- Start polling loop

### 2. Telegram Handlers (`bot/handlers.py`)

#### Quick Commands (predefined)
```
/status  → Combined overview (pods + queue + nodes)
/pods    → kubectl get pods -n jenkins
/nodes   → kubectl get nodes
/queue   → redis LIFTMASS key count + config
/dhcp    → SSH to KEA, check lease stats
/vms     → govc find VMs
/context → Show/switch K8s context
/logs <pod> → kubectl logs <pod> -n jenkins --tail=50
/help    → List all commands
```

#### AI Chat (freeform)
Any message without `/` prefix → sent to Claude AI with:
- System prompt containing full .clinerules context
- Conversation history (per-chat, last N messages)
- Tool use: AI can request command execution

### 3. AI Client (`bot/ai_client.py`)
- Anthropic Messages API client pointing to TrendMicro gateway
- System prompt with .clinerules infrastructure context
- Conversation memory (rolling window, last 20 messages)
- Streaming support (send partial responses for long operations)
- Tool definitions for command execution

### 4. Command Executor (`bot/executor.py`)
- Wraps subprocess for safe command execution
- Sets `KUBECONFIG=/home/rogueone/.kube/config-merged`
- Timeout handling (default 60s, configurable)
- Output truncation (Telegram message limit = 4096 chars)
- Command classification: read-only / mutating / destructive

### 5. Security (`bot/security.py`)
- **Chat ID whitelist**: Only respond to configured Telegram user(s)
- **Command classification**:
  - 🟢 Read-only: `get`, `describe`, `logs`, `status` → auto-execute
  - 🟡 Mutating: `scale`, `restart`, `rollout`, `hset` → show preview, require `/confirm`
  - 🔴 Destructive: `delete`, `kill`, `destroy`, `drain` → show warning, require `/confirm`
- **Pending confirmation queue**: Store last proposed command, wait for `/confirm` or `/cancel`
- **Rate limiting**: Max 30 requests per minute (prevent runaway)
- **Audit log**: All commands logged to `~/logs/telegram-agent.log`

## AI Tool Use Design

The AI can request to execute commands via a structured tool:

```json
{
  "name": "execute_command",
  "description": "Execute a shell command on the kube-controller",
  "input_schema": {
    "type": "object",
    "properties": {
      "command": {"type": "string", "description": "The shell command to execute"},
      "requires_confirmation": {"type": "boolean", "description": "Whether this needs user confirmation"}
    },
    "required": ["command"]
  }
}
```

Flow:
1. User asks question → AI receives it with system context
2. AI decides it needs to run a command → returns tool_use
3. Bot checks safety classification
4. If safe → execute and return result to AI
5. If destructive → ask user for confirmation first
6. AI receives output → formulates response
7. Bot sends final response to user

## Telegram Message Formatting

- Use Markdown for formatting (Telegram supports it)
- Code blocks for command output
- Truncate long outputs with "... (truncated, X more lines)"
- Split messages > 4096 chars into multiple messages

## Configuration

### Secrets file (`~/Documents/secrets/telegram-agent.env`)
```bash
# Telegram
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_CHAT_ID=<your chat ID>

# AI (from ~/.claude/settings.json)
ANTHROPIC_BASE_URL=https://api.rdsec.trendmicro.com/prod/aiendpoint/
ANTHROPIC_AUTH_TOKEN=<JWT token>
ANTHROPIC_MODEL=claude-4.6-sonnet-aws[1m]

# Infrastructure
KUBECONFIG=/home/rogueone/.kube/config-merged
```

## Systemd Service

```ini
[Unit]
Description=Gemini Telegram Bot
After=network.target

[Service]
Type=simple
User=rogueone
WorkingDirectory=/home/rogueone/Projects/gemini-telegram-bot
EnvironmentFile=/home/rogueone/Documents/secrets/telegram-agent.env
ExecStart=/usr/bin/python3 /home/rogueone/Projects/gemini-telegram-bot/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Token Renewal

The JWT token expires every ~3 months. When it expires:
1. Generate a new token from the company AI portal
2. Update `~/Documents/secrets/telegram-agent.env`
3. Restart: `sudo systemctl restart gemini-telegram-bot`

## Future Enhancements

- [ ] Multi-turn conversation with memory
- [ ] Scheduled status reports (daily morning summary)
- [ ] Alert forwarding (K8s events → Telegram notifications)
- [ ] Inline keyboard buttons for common actions
- [ ] Image support (send screenshots of Grafana dashboards)
- [ ] Voice message support (speech-to-text → AI → response)
