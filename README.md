# Gemini Telegram Bot 🤖📱

A Telegram bot that provides AI-powered operations access to the Lift infrastructure from your phone — no VPN, no laptop needed.

## Architecture

```
📱 Phone (Telegram, any network)
    │
    ▼ Send message
☁️  Telegram Cloud (public internet)
    │
    │  Bot polls for new messages (outbound HTTPS only)
    │  ⚡ No inbound ports, no exposed IP
    ▼
🖥️  kube-controller (10.209.x.x, internal network)
    ├── 🧠 AI: api.rdsec.trendmicro.com (Gemini API via Google AI)
    ├── ☸️  kubectl (all shard contexts)
    ├── 🔑 SSH to KEA DHCP / VMs
    ├── 🏗️  govc (vCenter operations)
    └── 🔧 Jenkins API (via port-forward)
    │
    ▼ Send response (outbound HTTPS)
☁️  Telegram Cloud → 📱 You receive the reply
```

## Key Design Decisions

- **Polling mode** (not webhook) — no need to expose any port on kube-controller
- **Gemini API via Google AI** (`api.rdsec.trendmicro.com`) — free, company-provided, Anthropic-compatible API
- **Chat ID whitelist** — only responds to your Telegram account
- **Confirmation for destructive ops** — delete/stop/kill commands require `/confirm`
- **System context** — `.clinerules` loaded as system prompt for full infrastructure awareness

## Quick Start

### 1. Create Telegram Bot

1. Open Telegram, find `@BotFather`
2. Send `/newbot`, follow prompts
3. Save the bot token

### 2. Get Your Chat ID

1. Send a message to your new bot
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find your `chat.id` in the response

### 3. Configure

```bash
cp config/secrets.env.template ~/Documents/secrets/telegram-agent.env
# Edit the file with your bot token and chat ID
vim ~/Documents/secrets/telegram-agent.env
```

### 4. Install Dependencies

```bash
cd ~/Projects/gemini-telegram-bot
pip install -r requirements.txt
```

### 5. Run

```bash
# Manual run (for testing)
python3 main.py

# Or install as systemd service (recommended)
sudo cp systemd/gemini-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gemini-telegram-bot
```

## Usage

### Quick Commands (shortcuts)

| Command | Description |
|---------|-------------|
| `/status` | Overview: pods, queue, DHCP, nodes |
| `/pods` | List Jenkins namespace pods |
| `/nodes` | Show K8s node status |
| `/queue` | Show Redis job queue count + config |
| `/dhcp` | Check KEA DHCP lease status |
| `/vms` | Count VMs in vCenter |
| `/context [name]` | Show/switch K8s context |
| `/help` | Show all commands |

### AI Chat (freeform)

Just type any message without a `/` prefix:

```
> Check why autopilot jobs are failing
> How many pods are running on worker3?
> What's the current concurrent_job_limit?
> Show me the last 5 jenkins slave pods that crashed
```

The AI has full knowledge of your `.clinerules` infrastructure context and can execute commands to investigate.

### Safety Controls

- 🟢 **Read-only commands** execute immediately
- 🟡 **Mutating commands** (scale, restart, patch) show preview + require `/confirm`
- 🔴 **Destructive commands** (delete, kill, destroy) show warning + require `/confirm`

## Files

```
gemini-telegram-bot/
├── README.md              # This file
├── PLAN.md                # Detailed design document
├── main.py                # Entry point
├── bot/
│   ├── __init__.py
│   ├── handlers.py        # Telegram command handlers
│   ├── ai_client.py       # Gemini API via Google AI client
│   ├── executor.py        # Safe command executor
│   └── security.py        # Auth, rate limiting, confirmation
├── config/
│   ├── secrets.env.template
│   └── system_prompt.md   # AI system context (from .clinerules)
├── systemd/
│   └── gemini-telegram-bot.service
├── requirements.txt
└── .gitignore
```

## Security

- Bot only responds to whitelisted Telegram chat IDs
- JWT token for AI endpoint stored in secrets file
- Destructive operations require explicit confirmation
- All commands are logged with timestamps
- No inbound ports opened on kube-controller
