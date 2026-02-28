# Telegram board developer runbook

## Start the Telegram board

From the repository root:

```bash
python3 src/main.py bot
```

## Auto-relaunch on code changes (recommended while iterating)

When you are changing `*.py` files in `src/` or `scripts/`, use the launcher so the bot always runs the latest code:

```bash
python3 scripts/run_telegram_board.py
```

### What it does

- Starts the bot with `python3 src/main.py bot`
- Watches `src/**/*.py` and `scripts/**/*.py` for edits
- Restarts the bot process when a change is detected
- Uses only Python standard library and `.env` loading already handled by the bot process

### Notes

- Export `TELEGRAM_BOT_TOKEN` (and optional `TELEGRAM_ALLOWED_CHAT_IDS`) before running.
- If Telegram requests fail with certificate validation errors, use:

  ```bash
  TELEGRAM_INSECURE_TLS=1 python3 scripts/run_telegram_board.py
  ```

- Stop the launcher with `Ctrl+C`; it will forward shutdown to the running bot.
