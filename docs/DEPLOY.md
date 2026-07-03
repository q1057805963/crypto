# Telegram and VPS Deployment Guide

This guide covers three things:

1. Configure Telegram push notifications
2. Run the project manually on a VPS
3. Keep it running with `systemd`

## 1. Telegram Configuration

The project already supports Telegram push through either `config.yaml` or environment variables.

### Recommended: environment variables

This keeps secrets out of the repository.

```bash
export CFM_TELEGRAM_ENABLED=true
export CFM_TELEGRAM_BOT_TOKEN="your_bot_token"
export CFM_TELEGRAM_CHAT_IDS="your_chat_id"
export CFM_TELEGRAM_BOT_RESPONDER_ENABLED=true
export CFM_TELEGRAM_BOT_REQUEST_TIMEOUT_SECONDS=20
export CFM_TELEGRAM_BOT_AI_COOLDOWN_SECONDS=20
```

Supported variables:

```bash
CFM_TELEGRAM_ENABLED=true
CFM_TELEGRAM_BOT_TOKEN=your_bot_token
CFM_TELEGRAM_CHAT_IDS=your_chat_id
CFM_TELEGRAM_BOT_RESPONDER_ENABLED=true
CFM_TELEGRAM_BOT_REQUEST_TIMEOUT_SECONDS=20
CFM_TELEGRAM_BOT_AI_COOLDOWN_SECONDS=20
CFM_EXCHANGE=okx_swap
CFM_DATA_SOURCE=rest
CFM_REST_POLL_INTERVAL_SECONDS=5
CFM_REST_DEPTH_POLL_INTERVAL_SECONDS=5
CFM_REST_LIQUIDATION_POLL_INTERVAL_SECONDS=15
CFM_DASHBOARD_HOST=0.0.0.0
CFM_DASHBOARD_PORT=8765
CFM_AUTH_ENABLED=true
CFM_AUTH_SECRET=replace_with_a_long_random_secret
CFM_AUTH_ALLOW_REGISTRATION=false
CFM_AUTH_TOKEN_TTL_SECONDS=604800
```

### Alternative: config.yaml

```yaml
telegram:
  enabled: true
  bot_token: "your bot token"
  chat_ids:
    - "your chat id"
```

### How to get the bot token

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Follow the prompts
4. Copy the bot token returned by BotFather

### How to get the chat id

For private messages:

1. Open your bot in Telegram
2. Send `/start`
3. Open:

```text
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
```

4. Find `chat.id` in the JSON response

For group messages:

1. Add the bot to the group
2. Send a message in the group
3. Call `getUpdates` again
4. Use the group's `chat.id` value, which is often negative

### Telegram bot questions

When `CFM_TELEGRAM_BOT_RESPONDER_ENABLED=true`, the app also polls Telegram `getUpdates` for bound bots. Bound chat IDs can ask questions such as:

```text
BTC now?
/ask ETH downside risk?
SOL 当前风险点是什么？
```

The bot only answers bound chat IDs, only uses that user's monitored symbols, and uses that user's AI configuration. If a Telegram webhook is already set on the same bot, clear it before using polling mode.

## 2. VPS Preparation

Recommended baseline:

- Ubuntu 22.04 or 24.04
- Python 3.11+
- 1 vCPU / 1 GB RAM is enough for this version

Install runtime dependencies:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Create a deployment directory:

```bash
cd /opt
sudo mkdir -p crypto-futures-monitor
sudo chown $USER:$USER crypto-futures-monitor
```

Clone or copy the project into:

```text
/opt/crypto-futures-monitor
```

Set up the Python environment:

```bash
cd /opt/crypto-futures-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Dashboard Exposure

For local-only access:

```yaml
dashboard:
  host: 127.0.0.1
  port: 8765
```

For direct remote access:

```yaml
dashboard:
  host: 0.0.0.0
  port: 8765
```

If you expose the port publicly, place it behind a firewall or reverse proxy. The safer default is to keep `127.0.0.1` and use Nginx/Caddy or an SSH tunnel.

JWT tokens are sent by the browser on every API request. On a real VPS, use HTTPS at the reverse proxy layer before opening this to the public internet.

## 4. Login and JWT

JWT login is enabled by default.

- First visit: create the first admin account in the browser.
- Later visits: log in with that account.
- Registration is closed after the first user unless `CFM_AUTH_ALLOW_REGISTRATION=true`.
- User passwords and JWT signing keys are stored under `data/`, which is ignored by Git.
- Each user's symbols, Telegram, AI and push threshold settings are stored separately by JWT user id.

For VPS deployment, set a stable secret:

```bash
openssl rand -base64 48
```

Put the generated value into:

```bash
CFM_AUTH_SECRET=your_generated_secret
```

Keep this value stable across restarts. If it changes, existing browser tokens will be invalidated and users need to log in again.

## 5. Manual Start

From the project root:

```bash
cd /opt/crypto-futures-monitor
source .venv/bin/activate
python main.py --config config.yaml
```

## 6. systemd Service

Templates already exist in:

- `deploy/linux/crypto-futures-monitor.service`
- `deploy/linux/crypto-futures-monitor.env.example`

Copy the environment template:

```bash
cp deploy/linux/crypto-futures-monitor.env.example deploy/linux/crypto-futures-monitor.env
```

Edit it and fill in real values:

```bash
CFM_TELEGRAM_ENABLED=true
CFM_TELEGRAM_BOT_TOKEN=your_bot_token
CFM_TELEGRAM_CHAT_IDS=your_chat_id
CFM_TELEGRAM_BOT_RESPONDER_ENABLED=true
CFM_TELEGRAM_BOT_REQUEST_TIMEOUT_SECONDS=20
CFM_TELEGRAM_BOT_AI_COOLDOWN_SECONDS=20
CFM_EXCHANGE=okx_swap
CFM_DATA_SOURCE=rest
CFM_REST_POLL_INTERVAL_SECONDS=5
CFM_REST_DEPTH_POLL_INTERVAL_SECONDS=5
CFM_REST_LIQUIDATION_POLL_INTERVAL_SECONDS=15
CFM_DASHBOARD_HOST=0.0.0.0
CFM_DASHBOARD_PORT=8765
CFM_AUTH_ENABLED=true
CFM_AUTH_SECRET=replace_with_a_long_random_secret
CFM_AUTH_ALLOW_REGISTRATION=false
```

Adjust the service file if needed:

- `User`
- `WorkingDirectory`
- `ExecStart`
- `EnvironmentFile`

Install and start the service:

```bash
sudo cp deploy/linux/crypto-futures-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable crypto-futures-monitor
sudo systemctl start crypto-futures-monitor
sudo systemctl status crypto-futures-monitor
```

Read logs:

```bash
journalctl -u crypto-futures-monitor -f
```

Restart after changing config:

```bash
sudo systemctl restart crypto-futures-monitor
```

## 7. Suggested Production Defaults

For a stable first deployment:

```yaml
exchange: okx_swap
data_source: rest
rest_poll_interval_seconds: 5
rest_per_symbol_delay_ms: 150
oi_poll_interval_seconds: 30
funding_poll_interval_seconds: 60
microstructure:
  rest_depth_poll_interval_seconds: 5
  rest_liquidation_poll_interval_seconds: 15
```

After the VPS is stable, you can evaluate moving from REST mode to WebSocket mode.

## 8. Binance HTTP 451

If Binance Futures returns HTTP 451 on the VPS, that is a server-side availability restriction from Binance. Do not rely on Binance Futures from that VPS. Use a reachable exchange data source instead:

```bash
CFM_EXCHANGE=okx_swap
CFM_DATA_SOURCE=rest
```

The OKX source supports ticker price, estimated 1-minute quote volume, open interest, funding rate, REST order-book depth and public liquidation-order polling. OKX liquidation data is lower frequency than Binance's force-order stream. The dashboard deduplicates returned orders and treats "no recent event" as a 1-minute window result, not proof that the whole market has zero liquidations.

## 9. What Not to Commit

Do not commit:

- `.venv/`
- `data/`
- `monitor.out`
- `monitor.err`
- `monitor.log`
- `deploy/linux/crypto-futures-monitor.env`

The repository already includes a `.gitignore` for these files.
