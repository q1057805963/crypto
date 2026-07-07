# Crypto Futures Monitor

USDT 永续合约异常波动监控 MVP，默认使用 OKX USDT Swap 数据源；Binance 适配代码保留但默认不启用。

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
- `exchange`: 交易所数据源，默认 `okx_swap`
- `data_source`: 数据传输方式，`websocket` 为 OKX 实时主行情，`rest` 为 OKX REST 轮询兜底
- `rest_poll_interval_seconds`: REST 轮询间隔
- `rest_per_symbol_delay_ms`: REST 模式下，每个合约请求之间的间隔
- `oi_poll_interval_seconds`: OI 拉取间隔
- `funding_poll_interval_seconds`: 资金费率拉取间隔
- `microstructure`: 爆仓流和盘口深度配置
- `window_seconds`: 长窗口，默认 300 秒
- `warmup_seconds`: 启动后先收集数据的时间
- `alert_cooldown_seconds`: 同一合约重复报警间隔
- `thresholds`: 异常判断阈值
- `thresholds.*_enabled`: 爆仓、点差、盘口失衡和深度下降等微结构信号是否参与异常评分
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

## 异常分计算方式

异常分不是交易所官方指标，也不是买卖建议。交易所提供的是成交、盘口、持仓量、资金费率、标记价、强平等原始数据；本系统把这些原始数据组合成 `0-100` 的异动雷达分。

当前评分采用“分项加权 + 共振加分”：

- 价格冲击：1m 波动和 5m 波动，最高约 32 分。只代表价格动了，不单独等于高质量信号。
- 量能冲击：1m 成交额相对近 5m 均值放大，最高约 18 分。
- 主动成交：taker 买入/卖出占比明显单边，最高约 15 分。
- 杠杆变化：OI 5m 变化和资金费率偏离，最高约 19 分。
- 强平冲击：1m 多空强平金额，最高约 12 分。
- 流动性风险：点差、盘口失衡、深度下降，最高约 18 分。
- 共振加分：价格与放量、主动成交、OI、强平方向、流动性变薄互相确认时额外加分。

这样设计的目的很明确：单一指标再夸张也不应该轻易满分；真正值得看的，是价格、成交、主动方向、持仓和流动性同时异常。

默认报警线是 `60` 分：

- `<45`: 观察，通常不推送
- `45-59`: 关注，说明有局部异动但证据还不够完整
- `60-79`: 风险预警，默认会触发报警
- `80+`: 极端异动，通常是多信号强共振或叠加强平/盘口变薄

默认信号参数：

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `price_move_pct_1m` | `0.6` | 1 分钟涨跌超过 0.6% 开始计入价格冲击 |
| `price_move_pct_5m` | `1.2` | 5 分钟涨跌超过 1.2% 开始计入趋势冲击 |
| `volume_multiplier` | `2.2` | 1 分钟成交额超过近 5 分钟均值 2.2 倍视为放量 |
| `taker_buy_ratio_high / low` | `0.68 / 0.32` | 主动买入或主动卖出明显单边 |
| `min_quote_volume_1m` | `300000` | 低于该成交额时价格波动降权，减少小币种噪音 |
| `oi_change_pct_5m` | `0.8` | 5 分钟持仓变化超过 0.8% 视为杠杆资金明显变化 |
| `funding_rate_abs` | `0.0003` | 资金费率绝对值超过 0.03% 视为多空拥挤参考 |
| `liquidation_quote_1m` | `75000` | 1 分钟强平超过 7.5 万 USDT 计入强平冲击 |
| `spread_bps` | `3.0` | 点差超过 3 bps 视为流动性变差 |
| `depth_imbalance_abs` | `0.22` | 买卖盘深度相差 22% 以上计入盘口失衡 |
| `depth_drop_pct_1m` | `15.0` | 1 分钟盘口深度下降 15% 以上计入插针风险 |

如果你不知道怎么设置，建议先不要频繁改全局参数。更实用的方式是：

- 大币种如 BTC、ETH：可以保持默认，或把单币报警线提高到 `65-70`，减少噪音。
- 活跃山寨如 SOL、DOGE、BNB：默认 `60` 更适合发现异动。
- 极低流动性币：优先提高 `min_quote_volume_1m` 或单币报警线，而不是把价格波动阈值调得很低。
- 想少打扰：单币规则设为 `异常分 >= 65`，并附加 `量能倍数 >= 2.2`。
- 想抓急拉急跌：单币规则用 `任一条件满足`，保留 `异常分 >= 60`，再加 `1分钟波动 >= 0.6` 或 `爆仓额 >= 75000`。

当前默认使用 OKX WebSocket 主行情，实时订阅成交、ticker、盘口、标记价、资金费率和持仓量。OKX REST 继续用于强平补偿、历史 K 线复盘和 WebSocket 异常时的兜底。

## 数据源策略

本项目当前采用 OKX-first：

- OKX WebSocket：主成交、主动买卖、ticker、盘口深度、标记价、资金费率和持仓量
- OKX REST：强平订单低频补偿、交易所 K 线复盘、合约规格换算和 WebSocket 兜底
- Binance：代码保留，默认配置不启用，后续有稳定线路时再切回

## 页面字段说明

- `采样数`: REST 模式下，表示过去 1 分钟采集到的行情快照数量。它不是交易所真实成交笔数，所以多个合约可能相同。
- `成交数`: WebSocket 模式下，表示过去 1 分钟收到的成交事件数量，更接近真实交易活跃度。
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
- 信号验证：按同币同向和同触发组合统计 15m 后效胜率、均值、顺向和逆向波动
- 风险边界：新报警会给出失效价、目标位、RR、边界依据和缓冲质量，缓冲会考虑结构/价值区、短线波动、tick 估算、标记价偏离和盘口深度
- 周期视图：按 `5m / 15m / 1h / 4h / 1d` 主动拉取交易所原生 K 线和标记价 K 线
- 多周期共振：汇总 `5m / 15m / 1h / 4h` 的结构状态、动能和 VWAP 偏离，输出方向、分数与冲突周期
- 结构位：结合摆动高低点、触碰聚类、成交密集区、POC 和 70% 价值区估算支撑/压力
- 微结构信号：爆仓流、轻量盘口深度、点差、深度失衡
- 信号调参：页面可单独开关爆仓、点差、盘口失衡和深度下降的评分链路并调整阈值
- AI 情景推演：AI 像交易台讲盘一样输出核心判断、关键证据、失效边界和观察信号，结构随数据自适应，并受到历史后效与多周期共振约束
- Telegram 推送：配置后自动发送异常提醒
- Telegram Bot 问答：绑定后可在 Bot 里询问已监控合约，系统结合当前指标调用 AI 回复

## 报警复盘口径

报警触发时，系统会记录当时价格作为锚点，并创建 `5m / 15m / 1h / 4h / 1d` 后效任务。任务到期后会优先查询当前交易所的原生 K 线，计算：

- 到期收盘相对报警价的变化，单位为 `bp`（`100 bp = 1%`）
- 从报警到到期之间的最高上冲和最低回撤
- 同窗口的标记价变化结果，写入复盘 payload，便于后续扩展展示

如果交易所 K 线查询失败，系统会回退到本地 `signal_snapshots` 采样快照，避免复盘任务永久停留在待回写状态。周期爆仓统计来自程序运行期间收集到的强平流，默认最多保留 1 天；刚启动时长周期爆仓可能偏少，价格和成交额 K 线不受影响。

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

面板默认启用登录。VPS 首次打开时会让你创建管理员账号，后续登录后才会加载个人监控、AI、Telegram 和阈值配置。每个账号的监控列表、推送、AI 和阈值设置按 JWT 用户独立保存。管理员在个人配置里可以查看系统用户列表，包括用户名、角色、监控合约数量、Telegram/AI 是否配置和单币规则数量；页面不会返回密码 hash、Bot Token 或 AI Key。`CFM_AUTH_SECRET` 请保持稳定，变更后已登录浏览器需要重新登录。

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
- 默认用 OKX WebSocket；如果 VPS 对 WebSocket 不稳定，再临时切到 `rest`
- 保留 `data/monitor.db`，后面复盘会很有用

## REST 风控建议

REST 模式只适合监控少量自选合约，不建议高频全市场扫描。

当前实现采用更保守的方式：

- 只请求页面中选择的合约
- 默认 5 秒一轮
- 每个合约请求之间默认间隔 150ms
- 用 `quoteVolume` 差值估算窗口成交额
- OKX WebSocket 提供成交和盘口；强平流可由 REST 低频补偿

推荐：

- 1-20 个合约：`rest_poll_interval_seconds: 5`
- 20-50 个合约：`rest_poll_interval_seconds: 10`
- 超过 50 个合约：优先使用 WebSocket 或分批扫描

不要把 REST 轮询调到 1 秒以下长期运行。专业部署时应优先使用 WebSocket 实时流，REST 只做补偿和低频指标。

这不是投资建议，也不会自动交易。第一版目标是尽早发现“某个合约正在变得不正常”。

## 待完善与专业化路线图

当前系统更适合做“异动发现 + 结构辅助 + 事后复盘”，还不应该被当成自动交易信号。按专业性粗略打分，当前大约在 `7-7.5/10`：数据链路、实时监控、微结构、周期结构、组合后效和多周期共振已经有基础，但仍缺少足够样本校准、订单流持续性验证和离线回测。

已完成的关键专业化项：

- 触发组合后效统计：报警会生成触发组合指纹，最近报警会展示同组合 15m 后效；AI 也会读取同币同向与同组合统计，并在低样本时降低结论强度。
- AI 情景推演输出：AI 不再套固定模板，而是围绕当前数据的核心驱动组织分析，必须给出核心判断、具体数字证据、失效边界和后续观察信号；Telegram 告警的观察建议也改为 AI 结合本次异动数据实时生成，模板建议仅作 AI 不可用时的兜底。
- 多周期共振评分：系统会把 `5m / 15m / 1h / 4h` 的结构状态、动能、VWAP 偏离和价值区位置合成方向、分数与冲突列表。
- 风险边界升级：失效价会从结构支撑/压力、价值区边缘、成交密集区、VWAP 和盘口墙中选择依据，并用短线波动、tick 估算、标记价偏离、盘口深度下降和深度/成交额比例生成缓冲。

下一批优先级最高的完善项：

- 盘口墙持续性与订单流：现在有轻量盘口深度和主动买卖比例，后续应记录买卖墙持续时间、撤单速度、CVD 或 taker delta。理由是很多急拉急跌不是看有没有挂单，而是看挂单是否持续、是否被主动单吃掉。难点是数据量更大，WebSocket 稳定性和内存控制要更谨慎。
- 样本回测和参数校准：需要增加离线回放脚本，读取历史 K 线/成交，批量跑报警逻辑并输出胜率、最大顺向、最大逆向、误报率。理由是阈值不能只靠感觉调。难点是交易所历史逐笔和盘口数据成本较高，只有 K 线回测会低估微结构风险。
- AI 证据约束继续深化：继续收紧 prompt，把低样本、数据缺失和周期冲突更明确地转成“不做强判断”。理由是 AI 的价值是总结证据和提示盲点，不是替代数据。
- 数据质量监控：继续增强 WebSocket 心跳、延迟、断流、REST 兜底和 source failover 可视化。理由是行情系统最怕数据断了但页面还像正常。难点是不同 VPS 网络环境差异很大，OKX/Binance 的区域连通性也不稳定。

可以暂缓或谨慎加入的内容：

- 太多技术指标：RSI、MACD、布林带等不是不能做，但如果不和当前订单流、结构、后效统计结合，很容易变成噪音。
- 直接给买卖点：当前系统应先做好“风险提示、结构边界、后效验证”，不建议直接输出开仓/平仓指令。
- 全市场高频扫描：REST 模式不适合全市场高频轮询，WebSocket 全市场也需要更严格的资源和限速设计。

判断后续功能是否值得做的标准：

- 它能不能减少误报，或者解释为什么这次报警值得看？
- 它能不能给出明确的失效条件，而不是只说“可能上涨/下跌”？
- 它能不能被后效统计验证，而不是只在图上看起来合理？
- 它在移动端是否足够清楚，不会让页面变成指标堆叠？
