# Gemini Telegram Bot

A personal Telegram bot powered by Google Gemini CLI — chat with Gemini from your phone, anywhere.

## Architecture

```
Phone (Telegram) --[message]--> Bot (Proxy) --[CLI + context]--> Workspace Brain (GEMINI.md)
                                     ^                               |
                                     |                               v
                               Background Summary <----------- Tools & Knowledge
```

## Key Design Decisions

- **Workspace-Native Brain** — The personality and rules live in `~/Workspace/GEMINI.md`, consistent across CLI and Bot.
- **Summary Buffer Memory** — Automated background summarization (every 2 messages) keeps context sharp without prompt bloat.
- **JSON Streaming** — Bot parses Gemini CLI's `stream-json` output to show real-time tool progress (Thinking, Reading, Searching) on Telegram.
- **Outbound-Only** — Polling mode ensures no open ports or public IPs are required.
- **YOLO Execution** — Bot operates in auto-approve mode for seamless mobile interaction.

## Quick Start

### 1. Create Telegram Bot

1. Open Telegram, find `@BotFather`
2. Send `/newbot`, follow prompts
3. Save the bot token

### 2. Get Your Chat ID

1. Send a message to your new bot
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find your `chat.id` in the response

### 3. Install Gemini CLI

```bash
nvm use 22
npm install -g @google/gemini-cli
gemini auth login
```

### 4. Configure

```bash
cp config/secrets.env.template ~/Documents/secrets/gemini-telegram-agent.env
vim ~/Documents/secrets/gemini-telegram-agent.env
```

### 5. Install Dependencies

```bash
pip install -r requirements.txt
```

### 6. Run

```bash
# Manual run (for testing)
python3 main.py

# Or install as systemd service (recommended)
sudo cp systemd/gemini-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gemini-telegram-bot
```

## Usage

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show commands |
| `/clear` | Clear conversation history |
| `/last_error` | Show last error details |

### Chat

Just type any message — it gets sent to Gemini CLI:

```
> What's the weather in Taipei?
> Help me write a Python script to parse CSV files
> Explain how DNS works
```

## Files

```
gemini-telegram-bot/
├── README.md
├── PLAN.md                # Design document
├── main.py                # Entry point
├── bot/
│   ├── __init__.py
│   ├── handlers.py        # Telegram command handlers
│   ├── ai_client.py       # Gemini CLI wrapper
│   ├── executor.py        # Subprocess executor
│   └── security.py        # Auth + rate limiting
├── config/
│   └── secrets.env.template
├── systemd/
│   └── gemini-telegram-bot.service
├── requirements.txt
└── .gitignore
```

## Security

- Bot only responds to whitelisted Telegram chat IDs
- Rate limited (30 req/min)
- No inbound ports opened
- No API keys stored — Gemini CLI uses Google account auth
