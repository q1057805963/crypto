"""交易所合约目录缓存（币安 USDT 永续 / OKX USDT 永续）。

用于两件事：
1. 面板保存监控列表时校验标的是否存在，两所均无的直接拒绝保存；
2. 数据源激活时过滤当前交易所没有的标的，避免无效订阅反复报错。

网络不可用时保留上一次成功的数据；从未成功拉取过则视为"未知"，
校验降级为放行并提示、不做过滤。

注意：合约代码不一定是 ASCII——币安期货真实存在中文命名的合约
（如 币安人生USDT、龙虾USDT），因此存在性只以交易所返回的目录为准。
"""

import json
import logging
import threading
import time
from urllib.request import Request, urlopen

_REFRESH_INTERVAL_SECONDS = 600.0
_RETRY_INTERVAL_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 15.0


def _get_json(url: str) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "crypto-futures-monitor/0.1",
        },
    )
    with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


class _InstrumentDirectory:
    label = ""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._symbols: set[str] | None = None
        self._fetched_at = 0.0
        self._last_attempt_at = 0.0

    def supports(self, symbol: str) -> bool | None:
        """True/False 为已知结论；None 表示目录尚不可用。"""
        with self._lock:
            if self._symbols is None:
                return None
            return str(symbol).upper() in self._symbols

    def available(self) -> bool:
        with self._lock:
            return self._symbols is not None

    def refresh_if_stale(self) -> None:
        now = time.time()
        with self._lock:
            fresh = (
                self._symbols is not None
                and now - self._fetched_at < _REFRESH_INTERVAL_SECONDS
            )
            if fresh or now - self._last_attempt_at < _RETRY_INTERVAL_SECONDS:
                return
            self._last_attempt_at = now
            had_data = self._symbols is not None
        try:
            symbols = self._fetch()
        except Exception as exc:
            logging.log(
                logging.DEBUG if had_data else logging.INFO,
                "%s合约目录刷新失败: %s（%s）",
                self.label,
                exc,
                "沿用上次数据" if had_data else "暂时无法校验该所标的",
            )
            return
        with self._lock:
            first_load = self._symbols is None
            self._symbols = symbols
            self._fetched_at = time.time()
        logging.log(
            logging.INFO if first_load else logging.DEBUG,
            "%s合约目录已加载，共 %d 个 USDT 永续",
            self.label,
            len(symbols),
        )

    def _fetch(self) -> set[str]:
        raise NotImplementedError


class BinanceUsdtPerpDirectory(_InstrumentDirectory):
    label = "币安"
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"

    def _fetch(self) -> set[str]:
        payload = _get_json(self.url)
        symbols = set()
        for item in payload.get("symbols", []):
            if str(item.get("contractType", "")).upper() != "PERPETUAL":
                continue
            if str(item.get("quoteAsset", "")).upper() != "USDT":
                continue
            if str(item.get("status", "")).upper() != "TRADING":
                continue
            name = str(item.get("symbol", "")).upper()
            if name:
                symbols.add(name)
        if not symbols:
            raise ValueError("exchangeInfo 未返回可交易合约")
        return symbols


class OkxUsdtSwapDirectory(_InstrumentDirectory):
    label = "OKX"
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"

    def _fetch(self) -> set[str]:
        payload = _get_json(self.url)
        if str(payload.get("code")) != "0":
            raise ValueError(payload.get("msg") or payload)
        symbols = set()
        for item in payload.get("data", []):
            if str(item.get("state", "live")).lower() != "live":
                continue
            parts = str(item.get("instId", "")).upper().split("-")
            if len(parts) == 3 and parts[1] == "USDT" and parts[2] == "SWAP":
                symbols.add(f"{parts[0]}{parts[1]}")
        if not symbols:
            raise ValueError("instruments 未返回可交易合约")
        return symbols


class InstrumentDirectories:
    def __init__(self) -> None:
        self.binance = BinanceUsdtPerpDirectory()
        self.okx = OkxUsdtSwapDirectory()
        self._refresher: threading.Thread | None = None

    def _directory_for(self, exchange: str) -> _InstrumentDirectory:
        if str(exchange).strip().lower().startswith("okx"):
            return self.okx
        return self.binance

    def exchange_label(self, exchange: str) -> str:
        return self._directory_for(exchange).label

    def refresh_all(self) -> None:
        for directory in (self.binance, self.okx):
            directory.refresh_if_stale()

    def start_background_refresh(self) -> None:
        if self._refresher and self._refresher.is_alive():
            return

        def worker() -> None:
            while True:
                try:
                    self.refresh_all()
                except Exception:
                    logging.exception("合约目录刷新线程异常")
                time.sleep(60)

        self._refresher = threading.Thread(
            target=worker,
            daemon=True,
            name="instrument-directories",
        )
        self._refresher.start()

    def filter_for_exchange(
        self, exchange: str, symbols: list[str]
    ) -> tuple[list[str], list[str]]:
        """返回 (可订阅, 跳过)。目录不可用时不做过滤。"""
        directory = self._directory_for(exchange)
        allowed: list[str] = []
        skipped: list[str] = []
        for symbol in symbols:
            name = str(symbol).upper()
            if directory.supports(name) is False:
                skipped.append(name)
            else:
                allowed.append(name)
        return allowed, skipped

    def validate(self, symbols: list[str], primary_exchange: str) -> dict:
        """校验待保存的标的（只读缓存，不触发网络请求）。

        missing: 两所均确认不存在，应拒绝保存；
        off_primary: 主数据源交易所没有、但另一所有，可保存但提示；
        unchecked: 目录不可用未能校验，可保存但提示。
        """
        primary = self._directory_for(primary_exchange)
        secondary = self.okx if primary is self.binance else self.binance
        missing: list[str] = []
        off_primary: list[str] = []
        unchecked: list[str] = []
        for symbol in symbols:
            name = str(symbol).upper()
            on_primary = primary.supports(name)
            on_secondary = secondary.supports(name)
            known = [value for value in (on_primary, on_secondary) if value is not None]
            if not known:
                unchecked.append(name)
            elif not any(known):
                # 只有一所确认没有、另一所未知时不武断拒绝
                if len(known) == 2:
                    missing.append(name)
                else:
                    unchecked.append(name)
            elif on_primary is False:
                off_primary.append(name)
        return {
            "missing": missing,
            "off_primary": off_primary,
            "unchecked": unchecked,
            "primary_label": primary.label,
            "secondary_label": secondary.label,
        }
