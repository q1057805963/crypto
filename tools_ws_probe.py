"""币安期货 WS 出口探针。

用法：切换 Clash 节点后运行 `python tools_ws_probe.py`，
10 秒内给出当前出口能否收到币安期货行情推送的结论。

背景：币安按出口 IP 地区限制期货行情推送（香港/日本/新加坡/美国等受限），
受限地区的表现是"连接成功、订阅成功、但零数据"。
"""

import asyncio
import json
import os
import sys
from urllib.request import getproxies

import websockets

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

for scheme, env_name in (("http", "HTTP_PROXY"), ("https", "HTTPS_PROXY")):
    url = getproxies().get(scheme)
    if url and not os.environ.get(env_name) and not os.environ.get(env_name.lower()):
        os.environ[env_name] = url

proxy = os.environ.get("HTTPS_PROXY") or "未检测到系统代理（TUN 模式下流量仍会经过 Clash）"


async def main() -> None:
    print(f"代理: {proxy}")
    print("连接 wss://fstream.binance.com/stream ...")
    try:
        async with websockets.connect(
            "wss://fstream.binance.com/stream",
            open_timeout=12,
        ) as websocket:
            await websocket.send(
                json.dumps({"method": "SUBSCRIBE", "params": ["btcusdt@aggTrade"], "id": 1})
            )
            deadline = asyncio.get_event_loop().time() + 10
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    print("✗ 订阅成功但 10 秒无行情 —— 当前出口地区被币安限制期货推送，请换节点")
                    print("  （香港/日本/新加坡/美国已知受限，可试台湾/韩国/马来西亚/土耳其/德国等）")
                    return
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                except (asyncio.TimeoutError, TimeoutError):
                    continue
                data = json.loads(message)
                if data.get("id") is not None:
                    continue
                trade = data.get("data", data)
                print(f"✓ 行情正常: {trade.get('s')} price={trade.get('p')} —— 该出口可用于币安 WS 主源")
                return
    except Exception as exc:
        print(f"✗ 连接失败: {type(exc).__name__}: {exc}")
        print("  多为代理不可用或被墙（未走代理）。确认 Clash 在运行且系统代理已开启。")


if __name__ == "__main__":
    asyncio.run(main())
