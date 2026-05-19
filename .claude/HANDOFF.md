# Handoff: Test Credential Fallback Feature

## Context

Commit `e599248` added multi-account credential fallback (`CredentialManager`) to `bot/ai_client.py`. The code is already on `master`. We verified that the `w54545254121` credential is valid (refresh_token works). A dry-run simulation confirmed the fallback logic works correctly in the log:

```
SIMULATION: Faking quota error for testing
Switched credentials to account: wilsonwkj
Quota hit, switching to account: wilsonwkj
```

However, the actual Gemini CLI call failed because the existing systemd bot instance was conflicting (Telegram polling conflict).

## TODO

- [ ] **Stop the existing systemd bot service**
  ```bash
  systemctl --user stop gemini-telegram-bot
  ```

- [ ] **Check the installed systemd unit file**
  ```bash
  ls -la ~/.config/systemd/user/gemini-telegram-bot.service
  systemctl --user status gemini-telegram-bot
  ```
  The unit file in the repo is at `systemd/gemini-telegram-bot.service`. If `~/.config/systemd/user/` already has it symlinked/copied from the same path, no need to recreate — just `daemon-reload` and `restart`.

- [ ] **If unit file not installed yet, install it**
  ```bash
  mkdir -p ~/.config/systemd/user
  cp ~/Workspace/gemini-telegram-bot/systemd/gemini-telegram-bot.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable gemini-telegram-bot
  ```

- [ ] **Start the bot and verify it runs clean**
  ```bash
  systemctl --user start gemini-telegram-bot
  journalctl --user -u gemini-telegram-bot -f
  ```
  Expected log: `Credential manager: 2 accounts available ['w54545254121', 'wilsonwkj']`

- [ ] **Test credential fallback (optional)**
  To do a full end-to-end test, you can either:
  1. Wait for a natural quota error, or
  2. Temporarily add simulation code (see below), restart, send a Telegram message, then revert

  **Simulation patch** (in `bot/ai_client.py`):
  - Add `_SIMULATE_QUOTA_ERROR = True` before `AIClient` class
  - Add `self._simulated_quota = False` in `__init__`
  - After the first `execute_streaming` call, inject:
    ```python
    if _SIMULATE_QUOTA_ERROR and not self._simulated_quota:
        self._simulated_quota = True
        result = CommandResult(command="(sim)", stdout="status: 429 RESOURCE_EXHAUSTED", stderr="", return_code=1)
    ```
  - Remember to revert after testing!

## Key Files

- `bot/ai_client.py` — CredentialManager + fallback logic
- `systemd/gemini-telegram-bot.service` — systemd unit file
- `~/.gemini/accounts/` — two accounts: `w54545254121`, `wilsonwkj`
