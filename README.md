# Crypto Futures Monitor

USDT 永续合约异常波动监控 MVP，支持 Binance U 本位合约和 OKX USDT Swap 数据源。

Deployment and Telegram guide:

- [docs/DEPLOY.md](/C:/Users/Admin/Documents/Codex/2026-07-02/wo-x/outputs/crypto-futures-monitor/docs/DEPLOY.md)

这个版本会读取交易所公开行情和微结构信号，监控配置里的 USDT 永续合约，按滚动窗口计算价格、成交量、持仓量、爆仓和盘口变化，并在控制台、Telegram 和本地 UI 输出异常提醒。

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
- `exchange`: 交易所数据源，`binance_usdm` 或 `okx_swap`
- `data_source`: 数据传输方式，`rest` 为 REST 轮询，`websocket` 目前只支持 Binance 推送流
- `rest_poll_interval_seconds`: REST 轮询间隔
- `rest_per_symbol_delay_ms`: REST 模式下，每个合约请求之间的间隔
- `oi_poll_interval_seconds`: OI 拉取间隔
- `funding_poll_interval_seconds`: 资金费率拉取间隔
- `microstructure`: 爆仓流和盘口深度配置
- `window_seconds`: 长窗口，默认 300 秒
- `warmup_seconds`: 启动后先收集数据的时间
- `alert_cooldown_seconds`: 同一合约重复报警间隔
- `thresholds`: 异常判断阈值
- `dashboard`: 本地 Web 面板配置，可调整端口或关闭
- `storage.snapshot_interval_seconds`: SQLite 快照落库间隔

## 当前异常逻辑

程序会综合这些信号：

- 1 分钟价格涨跌幅
- 5 分钟价格涨跌幅
- 1 分钟成交额相对近 5 分钟均值的放大倍数
- 1 分钟主动买入 / 主动卖出比例
- OI 5 分钟变化
- 资金费率偏离
- 1 分钟爆仓总额
- 盘口点差扩大
- 盘口深度下降
- 买卖盘深度失衡
- 最低 1 分钟成交额过滤，避免小额噪音

当前默认使用 REST 轮询源，适合本机网络对 WebSocket 推送不稳定的情况。切换到 `data_source: websocket` 后，主动买卖比例会更接近真实逐笔成交；该模式目前仅适用于 Binance。若 VPS 访问 Binance Futures 返回 HTTP 451，可设置 `exchange: okx_swap` 或环境变量 `CFM_EXCHANGE=okx_swap`，改用 OKX 合约公开行情源。

## Binance 451 处理

Binance Futures 如果返回 HTTP 451，通常是交易所按服务条款对该服务器线路或地区拒绝服务。程序不会绕过该限制，推荐切换到可正常访问的数据源：

```bash
CFM_EXCHANGE=okx_swap
CFM_DATA_SOURCE=rest
```

OKX 源支持主行情、1 分钟成交额估算、持仓量、资金费率、REST 盘口深度和公共强平订单统计。OKX 强平数据是 REST 低频补充，默认每 15 秒拉取一次并做去重；页面中的“近1m无”表示最近 1 分钟窗口内未捕获强平订单，不代表市场绝对没有强平。

## 页面字段说明

- `采样数`: REST 模式下，表示过去 1 分钟采集到的行情快照数量。它不是交易所真实成交笔数，所以多个合约可能相同。
- `成交数`: WebSocket 模式下，表示过去 1 分钟收到的聚合成交事件数量，更接近真实交易活跃度。
- `1分钟成交额`: REST 模式下用 `quoteVolume` 差值估算；WebSocket 模式下由逐笔聚合成交累加。
- `主动买入`: REST 模式下只能根据价格变化近似判断，WebSocket 模式下会使用成交方向字段。
- `OI 5分钟`: 持仓量在窗口内的变化。价格和 OI 同向增加时，更像新增资金推动；价格波动但 OI 下降时，更像平仓或短线脉冲。
- `资金费率`: 多空拥挤度参考。正值偏多拥挤，负值偏空拥挤；极端值要警惕反向清算或插针。
- `风险`: 根据价格、成交额、主动方向、OI、资金费率综合分层。
- `倾向`: 对当前异动的简短交易语义解释，例如“偏多：疑似新增资金推动”。
- `爆仓1m`: 最近 1 分钟的爆仓总额，能帮助识别踩踏和逼空。
- `点差`: 盘口最优买卖价之间的差异，过大通常意味着流动性变差。

## 专业化能力

当前版本已经从简单行情看板升级为可解释异动雷达：

- 方向倾向：偏多、偏空、拥挤、观察
- 风险等级：低风险、中风险、高风险、极高风险
- 置信度：根据触发原因数量和数据完整度估算
- 观察建议：给出下一步应该看什么，而不是直接给买卖建议
- 报警复盘：默认保存到 `data/monitor.db`
- 历史快照：默认保存 `signal_snapshots`，便于后续做复盘和阈值优化
- 后效回写：报警后自动补写 `5m / 15m / 1h / 4h / 1d` 的交易所 K 线复盘结果
- 复盘统计：页面汇总最近报警在 `5m / 15m / 1h / 4h / 1d` 的后效表现
- 周期视图：按 `5m / 15m / 1h / 4h / 1d` 主动拉取交易所原生 K 线和标记价 K 线
- 微结构信号：爆仓流、轻量盘口深度、点差、深度失衡
- Telegram 推送：配置后自动发送异常提醒
- Telegram Bot 问答：绑定后可在 Bot 里询问已监控合约，系统结合当前指标调用 AI 回复

## 报警复盘口径

报警触发时，系统会记录当时价格作为锚点，并创建 `5m / 15m / 1h / 4h / 1d` 后效任务。任务到期后会优先查询当前交易所的原生 K 线，计算：

- 到期收盘相对报警价的变化，单位为 `bp`（`100 bp = 1%`）
- 从报警到到期之间的最高上冲和最低回撤
- 同窗口的标记价变化结果，写入复盘 payload，便于后续扩展展示

如果交易所 K 线查询失败，系统会回退到本地 `signal_snapshots` 采样快照，避免复盘任务永久停留在待回写状态。周期爆仓统计来自程序运行期间收集到的强平流，默认最多保留 1 天；刚启动时长周期爆仓可能偏少，价格和成交额 K 线不受影响。

页面右侧详情抽屉会基于当前账号可见的最近报警生成复盘统计，按全部报警、上涨报警、下跌报警、震荡报警分组展示各周期的平均收盘变化、样本数和向上占比。

## Telegram 推送

编辑 `config.yaml`：

```yaml
telegram:
  enabled: true
  bot_token: '你的 Bot Token'
  chat_ids:
    - '你的 Chat ID'
```

重启程序后，达到报警阈值时会推送。

页面里的“发送测试”只验证 Bot Token 和 Chat ID 是否连通；真实报警推送仍然需要异常事件达到对应阈值。

更推荐在 VPS 上用环境变量覆盖敏感信息，代码已经支持：

```bash
CFM_TELEGRAM_ENABLED=true
CFM_TELEGRAM_BOT_TOKEN=你的_bot_token
CFM_TELEGRAM_CHAT_IDS=你的_chat_id
CFM_AUTH_ENABLED=true
CFM_AUTH_SECRET=一段足够长的随机密钥
```

### Telegram Bot 问答

启用后，可以直接在已绑定的 Bot 聊天里问：

```text
BTC 现在能追吗？
/ask ETH 会不会急跌？
SOL 当前风险点是什么？
```

Bot 只响应当前账号已绑定的 Chat ID，只允许查询该账号监控列表里的合约，并使用该账号自己的 AI 配置。若 AI 未开启或 API Key 未配置，Bot 会提示先到页面配置 AI。

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

面板默认启用登录。VPS 首次打开时会让你创建管理员账号，后续登录后才会加载个人监控、AI、Telegram 和阈值配置。每个账号的监控列表、推送、AI 和阈值设置按 JWT 用户独立保存。`CFM_AUTH_SECRET` 请保持稳定，变更后已登录浏览器需要重新登录。

如果面板对公网开放，请放到 Nginx/Caddy 反向代理后并启用 HTTPS；JWT 会随每次 API 请求发送，不能裸跑在公网 HTTP 上。

### systemd 常驻运行

项目里已经放了模板：

- [crypto-futures-monitor.service](/C:/Users/Admin/Documents/Codex/2026-07-02/wo-x/outputs/crypto-futures-monitor/deploy/linux/crypto-futures-monitor.service)
- [crypto-futures-monitor.env.example](/C:/Users/Admin/Documents/Codex/2026-07-02/wo-x/outputs/crypto-futures-monitor/deploy/linux/crypto-futures-monitor.env.example)

部署时：

1. 修改 `service` 文件里的 `User`、`WorkingDirectory`、`ExecStart`
2. 复制环境变量模板为真实 env 文件
3. 写入 Telegram token、chat ids 和 `CFM_AUTH_SECRET`
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
- 爆仓流和盘口深度走独立 WebSocket 辅助流

推荐：

- 1-20 个合约：`rest_poll_interval_seconds: 5`
- 20-50 个合约：`rest_poll_interval_seconds: 10`
- 超过 50 个合约：优先使用 WebSocket 或分批扫描

不要把 REST 轮询调到 1 秒以下长期运行。专业部署时应优先使用 WebSocket 实时流，REST 只做补偿和低频指标。

这不是投资建议，也不会自动交易。第一版目标是尽早发现“某个合约正在变得不正常”。

## 下一步

- 把 REST 主行情逐步切换为更完整的 WebSocket 实时流
- 增加深度异常和爆仓链路的单独开关与阈值调参界面
