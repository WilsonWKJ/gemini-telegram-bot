# Gemini Telegram Bot — Design Plan

## Problem Statement

I want a personal AI chatbot accessible from my phone via Telegram, powered by Google Gemini CLI. No API keys to manage, no token renewal — just authenticate with my Google account once and chat from anywhere.

## Solution

A Telegram bot that:
1. Accepts messages from Telegram (polling mode, outbound-only)
2. Routes them to Gemini CLI (`gemini -p --yolo`)
3. Returns results to Telegram

Gemini CLI handles its own authentication via the user's Google account and can execute commands, read files, and perform multi-step operations autonomously.

## Architecture

```
                    OUTBOUND ONLY (no ports exposed)
                    +------------------------------+
Phone (Telegram) -> |  Host machine                |
                    |  Bot polls api.telegram.org   |
                    |  gemini CLI (Google auth)     |
                    +------------------------------+
```

All connections are **outbound HTTPS**:
- `api.telegram.org` — Telegram Bot API (polling + sending messages)
- Google APIs — Gemini CLI's own authentication and inference

## Component Design

### 1. Main Entry Point (`main.py`)
- Load config from secrets file (Telegram token + chat ID only)
- Initialize Telegram bot (polling mode)
- Register command handlers
- Start polling loop

### 2. Telegram Handlers (`bot/handlers.py`)

#### Commands
```
/start   — Welcome message
/help    — Show commands
/clear   — Clear conversation history
/last_error — Show last error details
```

#### AI Chat (freeform)
Any message without `/` prefix -> sent to Gemini CLI via `gemini -p --yolo`

### 3. AI Client (`bot/ai_client.py`)
- Builds prompt with conversation context
- Calls `gemini -p '<prompt>' --yolo` via subprocess
- Maintains rolling conversation history (last 10 messages per chat)
- No API keys, no system prompt — Gemini CLI handles everything

### 4. Command Executor (`bot/executor.py`)
- Wraps subprocess for command execution
- Timeout handling (default 120s, 900s for Gemini CLI)
- Output truncation (Telegram message limit = 4096 chars)

### 5. Security (`bot/security.py`)
- **Chat ID whitelist**: Only respond to configured Telegram user(s)
- **Rate limiting**: Max 30 requests per minute

## Configuration

### Secrets file (`~/Documents/secrets/gemini-telegram-agent.env`)
```bash
# Telegram
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_CHAT_ID=<your chat ID>
```

No AI API keys needed — Gemini CLI authenticates via `gemini auth login`.

## Systemd Service

```ini
[Unit]
Description=Gemini Telegram Bot
After=network.target

[Service]
Type=simple
User=rogueone
WorkingDirectory=/home/rogueone/Projects/gemini-telegram-bot
EnvironmentFile=/home/rogueone/Documents/secrets/gemini-telegram-agent.env
ExecStart=/usr/bin/python3 /home/rogueone/Projects/gemini-telegram-bot/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Gemini CLI Setup

```bash
# Install via npm (requires Node 22)
nvm use 22
npm install -g @anthropic-ai/gemini-cli  # or however it's installed

# Authenticate (one-time)
gemini auth login
```

## Future Enhancements

- [ ] Multi-turn conversation with memory
- [ ] Scheduled tasks (daily summaries)
- [ ] Image support (send/receive images)
- [ ] Voice message support (speech-to-text)
- [ ] Inline keyboard buttons for common actions
