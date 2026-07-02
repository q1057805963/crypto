# Crypto Futures Monitor

Binance U 本位合约异常波动监控 MVP。

Deployment and Telegram guide:

- [docs/DEPLOY.md](/C:/Users/Admin/Documents/Codex/2026-07-02/wo-x/outputs/crypto-futures-monitor/docs/DEPLOY.md)

这个版本先做一件事：读取 Binance Futures 公开行情，监控配置里的 USDT 永续合约，按 1 分钟和 5 分钟滚动窗口计算价格、成交量、主动买卖失衡，并在控制台和本地 UI 输出异常提醒。

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 运行

```bash
python main.py --config config.yaml
```

默认会同时启动本地可视化 UI：

```text
http://127.0.0.1:8765
```

在页面顶部可以直接输入要监控的合约，例如：

```text
BTCUSDT, ETHUSDT, SOLUSDT
```

也可以只输入币种简称：

```text
BTC, ETH, SOL
```

保存后会立即切换监控列表，并写回 `config.yaml`。

## 配置

编辑 `config.yaml`：

- `symbols`: 要监控的合约，例如 `BTCUSDT`、`SOLUSDT`
- `data_source`: 数据源，`rest` 为 REST 轮询，`websocket` 为 Binance 推送流
- `rest_poll_interval_seconds`: REST 轮询间隔
- `rest_per_symbol_delay_ms`: REST 模式下，每个合约请求之间的间隔
- `window_seconds`: 长窗口，默认 300 秒
- `warmup_seconds`: 启动后先收集数据的时间
- `alert_cooldown_seconds`: 同一合约重复报警间隔
- `thresholds`: 异常判断阈值
- `dashboard`: 本地 Web 面板配置，可调整端口或关闭

## 当前异常逻辑

程序会综合这些信号：

- 1 分钟价格涨跌幅
- 5 分钟价格涨跌幅
- 1 分钟成交额相对近 5 分钟均值的放大倍数
- 1 分钟主动买入 / 主动卖出比例
- 最低 1 分钟成交额过滤，避免小额噪音

当前默认使用 REST 轮询源，适合本机网络对 Binance WebSocket 推送不稳定的情况。切换到 `data_source: websocket` 后，主动买卖比例会更接近真实逐笔成交。

## 页面字段说明

- `采样数`: REST 模式下，表示过去 1 分钟采集到的行情快照数量。它不是交易所真实成交笔数，所以多个合约可能相同。
- `成交数`: WebSocket 模式下，表示过去 1 分钟收到的聚合成交事件数量，更接近真实交易活跃度。
- `1分钟成交额`: REST 模式下用 `quoteVolume` 差值估算；WebSocket 模式下由逐笔聚合成交累加。
- `主动买入`: REST 模式下只能根据价格变化近似判断，WebSocket 模式下会使用成交方向字段。
- `OI 5分钟`: 持仓量在窗口内的变化。价格和 OI 同向增加时，更像新增资金推动；价格波动但 OI 下降时，更像平仓或短线脉冲。
- `资金费率`: 多空拥挤度参考。正值偏多拥挤，负值偏空拥挤；极端值要警惕反向清算或插针。
- `风险`: 根据价格、成交额、主动方向、OI、资金费率综合分层。
- `倾向`: 对当前异动的简短交易语义解释，例如“偏多：疑似新增资金推动”。

## 专业化能力

当前版本已经从简单行情看板升级为可解释异动雷达：

- 方向倾向：偏多、偏空、拥挤、观察
- 风险等级：低风险、中风险、高风险、极高风险
- 置信度：根据触发原因数量和数据完整度估算
- 观察建议：给出下一步应该看什么，而不是直接给买卖建议
- 报警复盘：默认保存到 `data/monitor.db`
- Telegram 推送：配置后自动发送异常提醒

## Telegram 推送

编辑 `config.yaml`：

```yaml
telegram:
  enabled: true
  bot_token: '你的 Bot Token'
  chat_id: '你的 Chat ID'
```

重启程序后，达到报警阈值时会推送。

更推荐在 VPS 上用环境变量覆盖敏感信息，代码已经支持：

```bash
CFM_TELEGRAM_ENABLED=true
CFM_TELEGRAM_BOT_TOKEN=你的_bot_token
CFM_TELEGRAM_CHAT_ID=你的_chat_id
```

### 获取 Bot Token

1. 在 Telegram 搜索 `@BotFather`
2. 发送 `/newbot`
3. 按提示创建机器人
4. 记录返回的 `bot token`

### 获取 Chat ID

私聊推送：

1. 在 Telegram 里打开你刚创建的机器人
2. 发送一条消息，比如 `/start`
3. 打开这个地址：

```text
https://api.telegram.org/bot<你的_bot_token>/getUpdates
```

4. 在返回内容里找到 `chat.id`

群组推送：

1. 把机器人拉进群
2. 给群发一条消息，或 `@机器人`
3. 再访问 `getUpdates`
4. 群组 `chat.id` 一般是负数

## VPS 部署

推荐环境：

- Ubuntu 22.04 或 24.04
- Python 3.11+
- 1C1G 就够跑第一版

部署步骤：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
cd /opt
sudo mkdir -p crypto-futures-monitor
sudo chown $USER:$USER crypto-futures-monitor
```

把项目放到 `/opt/crypto-futures-monitor` 之后执行：

```bash
cd /opt/crypto-futures-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果你想远程打开面板，把 [config.yaml](/C:/Users/Admin/Documents/Codex/2026-07-02/wo-x/outputs/crypto-futures-monitor/config.yaml) 里的：

```yaml
dashboard:
  host: 0.0.0.0
  port: 8765
```

然后放行端口，或者更推荐挂到 Nginx 反向代理后面。

### systemd 常驻运行

项目里已经放了模板：

- [crypto-futures-monitor.service](/C:/Users/Admin/Documents/Codex/2026-07-02/wo-x/outputs/crypto-futures-monitor/deploy/linux/crypto-futures-monitor.service)
- [crypto-futures-monitor.env.example](/C:/Users/Admin/Documents/Codex/2026-07-02/wo-x/outputs/crypto-futures-monitor/deploy/linux/crypto-futures-monitor.env.example)

部署时：

1. 修改 `service` 文件里的 `User`、`WorkingDirectory`、`ExecStart`
2. 复制环境变量模板为真实 env 文件
3. 写入 Telegram token 和 chat id
4. 安装并启动服务

命令如下：

```bash
sudo cp deploy/linux/crypto-futures-monitor.service /etc/systemd/system/
cp deploy/linux/crypto-futures-monitor.env.example deploy/linux/crypto-futures-monitor.env
sudo systemctl daemon-reload
sudo systemctl enable crypto-futures-monitor
sudo systemctl start crypto-futures-monitor
sudo systemctl status crypto-futures-monitor
```

查看日志：

```bash
journalctl -u crypto-futures-monitor -f
```

### 我建议你在 VPS 上额外做这几件事

- 先把 `dashboard.host` 设成 `127.0.0.1`，只通过 Nginx 或 SSH 隧道访问
- Telegram token 不要直接写进 Git 或公开仓库
- 先用 `rest` 模式跑稳定，再逐步切到 `websocket`
- 保留 `data/monitor.db`，后面复盘会很有用

## REST 风控建议

REST 模式只适合监控少量自选合约，不建议高频全市场扫描。

当前实现采用更保守的方式：

- 只请求页面中选择的合约
- 默认 5 秒一轮
- 每个合约请求之间默认间隔 150ms
- 用 `quoteVolume` 差值估算窗口成交额

推荐：

- 1-20 个合约：`rest_poll_interval_seconds: 5`
- 20-50 个合约：`rest_poll_interval_seconds: 10`
- 超过 50 个合约：优先使用 WebSocket 或分批扫描

不要把 REST 轮询调到 1 秒以下长期运行。专业部署时应优先使用 WebSocket 实时流，REST 只做补偿和低频指标。

这不是投资建议，也不会自动交易。第一版目标是尽早发现“某个合约正在变得不正常”。

## 下一步

- 接入持仓量 Open Interest
- 接入爆仓流
- 接入盘口深度变化
- 加 SQLite 保存历史信号
- 加 Telegram 或企业微信推送
