import asyncio

import websockets


async def main() -> None:
    async with websockets.connect(
        "wss://fstream.binance.com/ws/btcusdt@ticker",
        ping_interval=20,
        open_timeout=10,
    ) as websocket:
        message = await asyncio.wait_for(websocket.recv(), timeout=8)
        print(message)


if __name__ == "__main__":
    asyncio.run(main())
