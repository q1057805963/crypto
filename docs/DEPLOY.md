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
export CFM_TELEGRAM_CHAT_ID="your_chat_id"
```

Supported variables:

```bash
CFM_TELEGRAM_ENABLED=true
CFM_TELEGRAM_BOT_TOKEN=your_bot_token
CFM_TELEGRAM_CHAT_ID=your_chat_id
CFM_DASHBOARD_HOST=0.0.0.0
CFM_DASHBOARD_PORT=8765
```

### Alternative: config.yaml

```yaml
telegram:
  enabled: true
  bot_token: "your bot token"
  chat_id: "your chat id"
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

If you expose the port publicly, place it behind a firewall or reverse proxy. The safer default is to keep `127.0.0.1` and use Nginx or an SSH tunnel.

## 4. Manual Start

From the project root:

```bash
cd /opt/crypto-futures-monitor
source .venv/bin/activate
python main.py --config config.yaml
```

## 5. systemd Service

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
CFM_TELEGRAM_CHAT_ID=your_chat_id
CFM_DASHBOARD_HOST=0.0.0.0
CFM_DASHBOARD_PORT=8765
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

## 6. Suggested Production Defaults

For a stable first deployment:

```yaml
data_source: rest
rest_poll_interval_seconds: 5
rest_per_symbol_delay_ms: 150
oi_poll_interval_seconds: 30
funding_poll_interval_seconds: 60
```

After the VPS is stable, you can evaluate moving from REST mode to WebSocket mode.

## 7. What Not to Commit

Do not commit:

- `.venv/`
- `data/`
- `monitor.out`
- `monitor.err`
- `monitor.log`
- `deploy/linux/crypto-futures-monitor.env`

The repository already includes a `.gitignore` for these files.
