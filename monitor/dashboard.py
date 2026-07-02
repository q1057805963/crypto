import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time

from monitor.anomaly import AnomalyEvent, SymbolSnapshot


def normalize_symbols(symbols: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for raw_symbol in symbols:
        symbol = "".join(ch for ch in raw_symbol.upper().strip() if ch.isalnum())
        if not symbol:
            continue
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"
        if symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    return normalized


class DashboardState:
    def __init__(self, symbols: list[str], data_source: str) -> None:
        self._lock = threading.Lock()
        self._data_source = data_source
        self._symbols = {
            symbol: {
                "symbol": symbol,
                "score": 0,
                "direction": "waiting",
                "price": None,
                "updated_at": None,
                "price_move_pct_1m": 0,
                "price_move_pct_5m": 0,
                "quote_volume_1m": 0,
                "volume_multiplier": 0,
                "taker_buy_ratio_1m": 0.5,
                "trade_count_1m": 0,
                "open_interest": 0,
                "oi_change_pct_5m": 0,
                "funding_rate": 0,
                "spread_bps": 0,
                "depth_imbalance": 0,
                "bid_depth_notional": 0,
                "ask_depth_notional": 0,
                "depth_drop_pct_1m": 0,
                "long_liquidation_quote_1m": 0,
                "short_liquidation_quote_1m": 0,
                "liquidation_total_quote_1m": 0,
                "risk_level": "低风险",
                "bias": "观察：暂无明确方向",
                "confidence": 0,
                "reasons": [],
                "suggestions": [],
            }
            for symbol in normalize_symbols(symbols)
        }
        self._events: list[dict] = []

    def set_symbols(self, symbols: list[str]) -> None:
        with self._lock:
            normalized = normalize_symbols(symbols)
            self._symbols = {
                symbol: self._symbols.get(
                    symbol,
                    {
                        "symbol": symbol,
                        "score": 0,
                        "direction": "waiting",
                        "price": None,
                        "updated_at": None,
                        "price_move_pct_1m": 0,
                        "price_move_pct_5m": 0,
                        "quote_volume_1m": 0,
                        "volume_multiplier": 0,
                        "taker_buy_ratio_1m": 0.5,
                        "trade_count_1m": 0,
                        "open_interest": 0,
                        "oi_change_pct_5m": 0,
                        "funding_rate": 0,
                        "spread_bps": 0,
                        "depth_imbalance": 0,
                        "bid_depth_notional": 0,
                        "ask_depth_notional": 0,
                        "depth_drop_pct_1m": 0,
                        "long_liquidation_quote_1m": 0,
                        "short_liquidation_quote_1m": 0,
                        "liquidation_total_quote_1m": 0,
                        "risk_level": "低风险",
                        "bias": "观察：暂无明确方向",
                        "confidence": 0,
                        "reasons": [],
                        "suggestions": [],
                    },
                )
                for symbol in normalized
            }

    def update_snapshot(self, snapshot: SymbolSnapshot) -> None:
        with self._lock:
            data = asdict(snapshot)
            data["reasons"] = list(data["reasons"])
            data["suggestions"] = list(data["suggestions"])
            self._symbols[snapshot.symbol] = data

    def set_events(self, events: list[dict]) -> None:
        with self._lock:
            self._events = events[:50]

    def add_event(self, event: AnomalyEvent) -> None:
        with self._lock:
            data = asdict(event)
            data["reasons"] = list(data["reasons"])
            data["suggestions"] = list(data["suggestions"])
            data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._events.insert(0, data)
            self._events = self._events[:50]

    def as_payload(self) -> dict:
        with self._lock:
            symbols = list(self._symbols.values())
            symbols.sort(key=lambda item: (-float(item.get("score") or 0), item["symbol"]))
            return {
                "generated_at": time(),
                "data_source": self._data_source,
                "symbols": symbols,
                "events": list(self._events),
            }


class DashboardServer:
    def __init__(
        self,
        state: DashboardState,
        host: str,
        port: int,
        on_symbols_change,
    ) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.on_symbols_change = on_symbols_change
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logging.info("Dashboard available at http://%s:%s", self.host, self.port)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        state = self.state
        on_symbols_change = self.on_symbols_change

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/" or self.path.startswith("/index.html"):
                    self._send_text(INDEX_HTML, "text/html; charset=utf-8")
                    return

                if self.path.startswith("/api/state"):
                    body = json.dumps(state.as_payload()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_error(404)

            def do_POST(self) -> None:
                if not self.path.startswith("/api/symbols"):
                    self.send_error(404)
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length).decode("utf-8")
                    payload = json.loads(body)
                    symbols = normalize_symbols(payload.get("symbols", []))
                    if not symbols:
                        raise ValueError("symbols cannot be empty")
                    on_symbols_change(symbols)
                    response = json.dumps({"ok": True, "symbols": symbols}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                except Exception as exc:
                    response = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send_text(self, text: str, content_type: str) -> None:
                body = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>合约异动监控</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #111316;
      --panel: #1a1d22;
      --panel-2: #20252b;
      --text: #e8edf2;
      --muted: #8d99a6;
      --line: #303741;
      --green: #2bd576;
      --red: #ff5a66;
      --amber: #f2b84b;
      --blue: #64a8ff;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      letter-spacing: 0;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: #15181c;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 650;
    }

    .status {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .toolbar {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) auto;
      gap: 10px;
      width: min(680px, 100%);
    }

    .symbol-input {
      width: 100%;
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #101215;
      color: var(--text);
      padding: 0 11px;
      outline: none;
      font-size: 13px;
    }

    .symbol-input:focus {
      border-color: var(--blue);
    }

    .save-btn {
      height: 36px;
      border: 1px solid #3a4655;
      border-radius: 6px;
      background: #243244;
      color: var(--text);
      padding: 0 14px;
      font-size: 13px;
      cursor: pointer;
    }

    .save-btn:hover {
      border-color: var(--blue);
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 0 4px rgba(43, 213, 118, .12);
    }

    main {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 380px;
      gap: 18px;
      padding: 18px;
    }

    section {
      min-width: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }

    .section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }

    th, td {
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      text-align: right;
      font-size: 13px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    th {
      color: var(--muted);
      font-weight: 550;
      background: var(--panel-2);
    }

    th:first-child, td:first-child { text-align: left; }
    tr:last-child td { border-bottom: 0; }
    tbody tr {
      cursor: pointer;
    }
    tbody tr:hover {
      background: rgba(100, 168, 255, .05);
    }
    tbody tr.selected {
      background: rgba(100, 168, 255, .08);
    }
    .symbol { font-weight: 700; }
    .cell-sub {
      margin-top: 4px;
      max-width: 120px;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--muted);
      font-size: 12px;
    }
    .muted { color: var(--muted); }
    .up { color: var(--green); }
    .down { color: var(--red); }
    .mixed { color: var(--amber); }
    .score {
      display: inline-flex;
      justify-content: center;
      min-width: 44px;
      padding: 4px 7px;
      border-radius: 6px;
      background: #242a31;
      color: var(--blue);
      font-weight: 700;
    }

    .risk {
      display: inline-flex;
      justify-content: center;
      min-width: 64px;
      padding: 4px 7px;
      border-radius: 6px;
      background: #242a31;
      font-weight: 700;
    }

    .risk-low { color: var(--muted); }
    .risk-mid { color: var(--amber); }
    .risk-high { color: var(--red); }

    .tag {
      display: inline-flex;
      justify-content: center;
      min-width: 42px;
      padding: 4px 7px;
      border-radius: 6px;
      background: #242a31;
      color: var(--text);
      font-weight: 700;
    }

    .bias-up { color: var(--green); }
    .bias-down { color: var(--red); }
    .bias-watch { color: var(--muted); }
    .bias-crowded { color: var(--amber); }

    .events {
      display: flex;
      flex-direction: column;
    }

    .detail {
      padding: 16px;
      border-bottom: 1px solid var(--line);
    }

    .detail-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }

    .detail-symbol {
      font-size: 18px;
      font-weight: 750;
    }

    .detail-bias {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    .metric-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin: 12px 0;
    }

    .metric {
      min-width: 0;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #171a1f;
    }

    .metric-label {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 6px;
    }

    .metric-value {
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }

    .detail-block {
      margin-top: 12px;
    }

    .detail-title {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }

    .detail-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      color: var(--text);
      font-size: 12px;
      line-height: 1.45;
    }

    .event-list {
      overflow: auto;
      max-height: calc(100vh - 520px);
    }

    .event {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }

    .event-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
    }

    .event-title {
      font-size: 14px;
      font-weight: 700;
    }

    .event-meta {
      color: var(--muted);
      font-size: 12px;
    }

    .reason {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }

    .empty {
      padding: 22px 16px;
      color: var(--muted);
      font-size: 13px;
    }

    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .event-list { max-height: none; }
    }

    @media (max-width: 720px) {
      header {
        align-items: flex-start;
        flex-direction: column;
      }
      main { padding: 10px; }
      section { border-radius: 6px; }
      .table-wrap { overflow-x: auto; }
      table { min-width: 1080px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>合约异动监控</h1>
    <div class="toolbar">
      <input id="symbol-input" class="symbol-input" autocomplete="off" spellcheck="false" placeholder="BTCUSDT, ETHUSDT, SOLUSDT">
      <button id="save-symbols" class="save-btn">保存监控</button>
    </div>
    <div class="status"><span class="dot"></span><span id="updated">等待数据</span></div>
  </header>

  <main>
    <section>
      <div class="section-title">
        <span>USDT 永续合约</span>
        <span id="count">0 个合约</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>异常分</th>
              <th>风险</th>
              <th>倾向</th>
              <th>价格</th>
              <th>1分钟</th>
              <th>5分钟</th>
              <th>1分钟成交额</th>
              <th>放大倍数</th>
              <th>OI 5分钟</th>
              <th>爆仓1m</th>
              <th>点差</th>
            </tr>
          </thead>
          <tbody id="symbols"></tbody>
        </table>
      </div>
    </section>

    <section class="events">
      <div class="section-title">
        <span>合约详情</span>
        <span id="source-label">REST</span>
      </div>
      <div class="detail" id="detail"></div>
      <div class="section-title">
        <span>最近报警</span>
        <span id="alert-count">0</span>
      </div>
      <div class="event-list" id="events"></div>
    </section>
  </main>

  <script>
    const symbolsEl = document.getElementById("symbols");
    const eventsEl = document.getElementById("events");
    const updatedEl = document.getElementById("updated");
    const countEl = document.getElementById("count");
    const alertCountEl = document.getElementById("alert-count");
    const symbolInputEl = document.getElementById("symbol-input");
    const saveSymbolsEl = document.getElementById("save-symbols");
    const detailEl = document.getElementById("detail");
    const sourceLabelEl = document.getElementById("source-label");
    let selectedSymbol = null;
    let inputTouched = false;

    const directionText = {
      up: "向上异动",
      down: "向下异动",
      mixed: "混合异常",
      waiting: "等待数据"
    };

    function fmtNumber(value, digits = 2) {
      if (value === null || value === undefined) return "--";
      return Number(value).toLocaleString(undefined, {
        maximumFractionDigits: digits,
        minimumFractionDigits: 0
      });
    }

    function fmtPct(value) {
      const number = Number(value || 0);
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(3)}%</span>`;
    }

    function fmtFunding(value) {
      const number = Number(value || 0) * 100;
      const cls = number > 0 ? "up" : number < 0 ? "down" : "muted";
      return `<span class="${cls}">${number >= 0 ? "+" : ""}${number.toFixed(4)}%</span>`;
    }

    function fmtBps(value) {
      const number = Number(value || 0);
      const cls = number >= 4 ? "down" : number >= 2 ? "mixed" : "muted";
      return `<span class="${cls}">${number.toFixed(2)}</span>`;
    }

    function riskClass(level) {
      if (level && level.includes("高")) return "risk-high";
      if (level && level.includes("中")) return "risk-mid";
      return "risk-low";
    }

    function shortBias(bias) {
      const text = bias || "观察";
      if (text.includes("偏多")) return "偏多";
      if (text.includes("偏空")) return "偏空";
      if (text.includes("拥挤")) return "拥挤";
      if (text.includes("波动")) return "波动";
      return "观察";
    }

    function biasClass(bias) {
      const text = shortBias(bias);
      if (text === "偏多") return "bias-up";
      if (text === "偏空") return "bias-down";
      if (text === "拥挤" || text === "波动") return "bias-crowded";
      return "bias-watch";
    }

    function signalTag(symbol) {
      if (Number(symbol.score || 0) >= 70) return "报警";
      if (Number(symbol.score || 0) >= 45) return "关注";
      if (Math.abs(Number(symbol.price_move_pct_1m || 0)) >= 0.8) return "急动";
      if (Number(symbol.volume_multiplier || 0) >= 3) return "放量";
      if (Math.abs(Number(symbol.oi_change_pct_5m || 0)) >= 0.3) return "OI";
      if (Number(symbol.liquidation_total_quote_1m || 0) >= 250000) return "爆仓";
      if (Number(symbol.spread_bps || 0) >= 4 || Number(symbol.depth_drop_pct_1m || 0) >= 18) return "盘口";
      if (Math.abs(Number(symbol.funding_rate || 0)) >= 0.0005) return "费率";
      if (Number(symbol.score || 0) > 0) return "监测";
      return "静默";
    }

    function rowClass(symbol) {
      if (symbol.direction === "up") return "up";
      if (symbol.direction === "down") return "down";
      if (symbol.direction === "mixed") return "mixed";
      return "muted";
    }

    function renderSymbols(symbols) {
      countEl.textContent = `${symbols.length} 个合约`;
      if (!selectedSymbol && symbols.length) {
        selectedSymbol = symbols[0].symbol;
      }
      if (!inputTouched) {
        symbolInputEl.value = symbols.map((symbol) => symbol.symbol).join(", ");
      }
      symbolsEl.innerHTML = symbols.map((symbol) => `
        <tr data-symbol="${symbol.symbol}" class="${symbol.symbol === selectedSymbol ? "selected" : ""}">
          <td>
            <div class="symbol">${symbol.symbol}</div>
            <div class="cell-sub">${signalTag(symbol)}</div>
          </td>
          <td><span class="score">${fmtNumber(symbol.score, 1)}</span></td>
          <td><span class="risk ${riskClass(symbol.risk_level)}">${symbol.risk_level || "低风险"}</span></td>
          <td><span class="tag ${biasClass(symbol.bias)}">${shortBias(symbol.bias)}</span></td>
          <td>${fmtNumber(symbol.price, 8)}</td>
          <td>${fmtPct(symbol.price_move_pct_1m)}</td>
          <td>${fmtPct(symbol.price_move_pct_5m)}</td>
          <td>${fmtNumber(symbol.quote_volume_1m, 0)}</td>
          <td class="${rowClass(symbol)}">${fmtNumber(symbol.volume_multiplier, 2)}x</td>
          <td>${fmtPct(symbol.oi_change_pct_5m)}</td>
          <td>${fmtNumber(symbol.liquidation_total_quote_1m, 0)}</td>
          <td>${fmtBps(symbol.spread_bps)}</td>
        </tr>
      `).join("");

      symbolsEl.querySelectorAll("tr").forEach((row) => {
        row.addEventListener("click", () => {
          selectedSymbol = row.dataset.symbol;
          renderSymbols(symbols);
          renderDetail(symbols);
        });
      });
    }

    function renderDetail(symbols) {
      const symbol = symbols.find((item) => item.symbol === selectedSymbol) || symbols[0];
      if (!symbol) {
        detailEl.innerHTML = `<div class="empty">等待行情数据</div>`;
        return;
      }

      const reasons = (symbol.reasons || []).length ? symbol.reasons : ["暂无明确触发项"];
      const suggestions = (symbol.suggestions || []).length ? symbol.suggestions : ["保持观察，等待价格、量能或 OI 形成共振"];
      detailEl.innerHTML = `
        <div class="detail-head">
          <div>
            <div class="detail-symbol">${symbol.symbol}</div>
            <div class="detail-bias">${symbol.bias || "观察：暂无明确方向"}</div>
          </div>
          <span class="score">${fmtNumber(symbol.score, 1)}</span>
        </div>
        <div class="metric-grid">
          <div class="metric"><div class="metric-label">风险</div><div class="metric-value ${riskClass(symbol.risk_level)}">${symbol.risk_level || "低风险"}</div></div>
          <div class="metric"><div class="metric-label">置信度</div><div class="metric-value">${fmtNumber(symbol.confidence, 1)}%</div></div>
          <div class="metric"><div class="metric-label">1分钟成交额</div><div class="metric-value">${fmtNumber(symbol.quote_volume_1m, 0)}</div></div>
          <div class="metric"><div class="metric-label">量能倍数</div><div class="metric-value ${rowClass(symbol)}">${fmtNumber(symbol.volume_multiplier, 2)}x</div></div>
          <div class="metric"><div class="metric-label">OI 5分钟</div><div class="metric-value">${fmtPct(symbol.oi_change_pct_5m)}</div></div>
          <div class="metric"><div class="metric-label">资金费率</div><div class="metric-value">${fmtFunding(symbol.funding_rate)}</div></div>
          <div class="metric"><div class="metric-label">多头爆仓 1m</div><div class="metric-value">${fmtNumber(symbol.long_liquidation_quote_1m, 0)}</div></div>
          <div class="metric"><div class="metric-label">空头爆仓 1m</div><div class="metric-value">${fmtNumber(symbol.short_liquidation_quote_1m, 0)}</div></div>
          <div class="metric"><div class="metric-label">盘口点差</div><div class="metric-value">${fmtBps(symbol.spread_bps)} bps</div></div>
          <div class="metric"><div class="metric-label">盘口深度下降</div><div class="metric-value">${fmtNumber(symbol.depth_drop_pct_1m, 1)}%</div></div>
          <div class="metric"><div class="metric-label">买盘深度</div><div class="metric-value">${fmtNumber(symbol.bid_depth_notional, 0)}</div></div>
          <div class="metric"><div class="metric-label">卖盘深度</div><div class="metric-value">${fmtNumber(symbol.ask_depth_notional, 0)}</div></div>
          <div class="metric"><div class="metric-label">盘口失衡</div><div class="metric-value">${fmtNumber(Number(symbol.depth_imbalance || 0) * 100, 1)}%</div></div>
          <div class="metric"><div class="metric-label">主动买入</div><div class="metric-value">${fmtNumber(Number(symbol.taker_buy_ratio_1m || 0) * 100, 1)}%</div></div>
        </div>
        <div class="detail-block">
          <div class="detail-title">触发原因</div>
          <div class="detail-list">${reasons.map((item) => `<div>${item}</div>`).join("")}</div>
        </div>
        <div class="detail-block">
          <div class="detail-title">观察建议</div>
          <div class="detail-list">${suggestions.map((item) => `<div>${item}</div>`).join("")}</div>
        </div>
      `;
    }

    function renderEvents(events) {
      alertCountEl.textContent = String(events.length);
      if (!events.length) {
        eventsEl.innerHTML = `<div class="empty">暂无报警</div>`;
        return;
      }
      eventsEl.innerHTML = events.map((event) => `
        <div class="event">
          <div class="event-head">
            <div>
              <div class="event-title ${event.direction}">${event.symbol} 异常分 ${event.score}/100</div>
              <div class="event-meta">${event.created_at} · ${event.risk_level || ""} · ${event.bias || ""}</div>
            </div>
            <span class="score">${directionText[event.direction] || event.direction}</span>
          </div>
          <div class="reason">${(event.reasons || []).join("; ")}</div>
          <div class="reason">观察：${(event.suggestions || []).join("; ")}</div>
        </div>
      `).join("");
    }

    async function refresh() {
      try {
        const response = await fetch("/api/state", { cache: "no-store" });
        const data = await response.json();
        sourceLabelEl.textContent = data.data_source === "websocket" ? "WebSocket" : "REST";
        renderSymbols(data.symbols || []);
        renderDetail(data.symbols || []);
        renderEvents(data.events || []);
        updatedEl.textContent = `已更新 ${new Date().toLocaleTimeString()}`;
      } catch (error) {
        updatedEl.textContent = "面板连接中断";
      }
    }

    async function saveSymbols() {
      const symbols = symbolInputEl.value
        .split(/[\\s,，;；]+/)
        .map((symbol) => symbol.trim())
        .filter(Boolean);

      saveSymbolsEl.disabled = true;
      saveSymbolsEl.textContent = "保存中";
      try {
        const response = await fetch("/api/symbols", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbols })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || "保存失败");
        }
        inputTouched = false;
        await refresh();
        updatedEl.textContent = "监控列表已更新";
      } catch (error) {
        updatedEl.textContent = error.message || "保存失败";
      } finally {
        saveSymbolsEl.disabled = false;
        saveSymbolsEl.textContent = "保存监控";
      }
    }

    symbolInputEl.addEventListener("input", () => {
      inputTouched = true;
    });
    symbolInputEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        saveSymbols();
      }
    });
    saveSymbolsEl.addEventListener("click", saveSymbols);

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""
