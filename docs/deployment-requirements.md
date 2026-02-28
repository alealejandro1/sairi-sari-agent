# Deployment and hosting requirements (MVP-lite)

This section estimates the smallest practical hardware and hosting setup for the current system.

## What this project currently needs

- Runtime: Python 3.11 bot process.
- Process model: one long-running process (`python3 src/main.py bot`) using Telegram polling by default.
- Persistence: local JSON file at `data/business_state.json` (lightweight single-tenant state).
- OCR: PaddleOCR for image parsing (`extract_text_from_image` in `src/ledger_ocr.py`).
- Storage: filesystem persistence (no database server).

## Minimal local development machine

For local dev/testing:

- CPU: dual-core laptop CPU (x86_64 or Apple Silicon).
- RAM: 4 GB minimum; 8 GB recommended for OCR model load.
- Disk: 10 GB free.
- Internet: stable broadband for Telegram webhooks/OCR downloads.
- OS: macOS, Linux, or Windows with Python 3.11+ and `pip`.

If OCR is not installed, this still runs with fewer resources (mostly text parsing and JSON updates).

## Minimal production-style host (small shop use)

Assume a few thousand OCR/text rows total and light daily traffic.

- CPU: 1 vCPU is workable for text-first + low photo volume; 2 vCPU is safer with frequent photos.
- RAM: 2 GB minimum for plain bot logic; 4 GB recommended with live PaddleOCR usage.
- Disk: 5–10 GB minimum with volume persistence.
- Bandwidth: tiny (except image downloads for OCR).

For reliability, keep OCR and bot files on a persistent disk, not ephemeral instance storage.

## Cloud hosting options (low-cost, practical)

The cheapest options for this architecture are:

- **Hetzner Cloud / Oracle Cloud Always Free VM + small instance**
  - Strongest cost/performance for always-on background workers.
  - SSH-based ops, manual service setup (`systemd`, Docker).

- **DigitalOcean App Platform / Render / Fly.io Worker-style container**
  - Fast to start, simple deploys.
  - Check minimum RAM plan includes enough room for OCR model warmup.

- **Railway / Render background worker**
  - Easy config and environment management.
  - Be careful on free/sleepy plans: polling bots need persistent uptime.

- **VPS (Linode, Vultr, Lightsail, etc.)**
  - Standard small VMs are a good fit if you want full control.

### What I would choose first

1) If you want lowest cost with predictable uptime: **small Linux VM + Docker/systemd**.
2) If you want easiest setup and no server management: **a managed container worker** with 2 GB RAM minimum.

## Deployment notes for current design

- Keep at least one persistent directory mounted for:
  - `data/business_state.json`
  - `logs/telegram_conversations.jsonl`

- Keep secrets in environment variables (`TELEGRAM_BOT_TOKEN`, `BUSINESS_STATE_STORE_PATH`, `TELEGRAM_ALLOWED_CHAT_IDS`).
- Prefer polling initially. Webhooks require public HTTPS and inbound handling if you later switch.
- If PaddleOCR install is too heavy on cloud, the bot still works without image OCR if dependency is absent; ledger-photo parsing will fail with the “install OCR” message until dependency is added.

## Capacity outlook (this version)

- Thousands of entries is fine in JSON mode.
- Bottlenecks before that point are usually OCR model latency and disk-write safety (single-file writes), not CPU.
- If traffic grows beyond one-shop scale, move to a managed DB next and keep the same JSON-like section boundaries.

## Suggested upgrade thresholds

- Move from 1 GB RAM plans to 2–4 GB once photos increase materially.
- Move from local JSON to SQL/managed DB when you expect concurrent users or large historical queries.
- Add periodic backups of `data/business_state.json` once daily.
