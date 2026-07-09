"""
Binance Futures WS node probe — batch test which Clash nodes can receive fstream data.

Usage:
    python tools_node_probe.py

Automatically:
1. Reads Crypto group members from Clash controller
2. Picks one representative node per region (skips known-blocked: HK, JP, SG, US, KR)
3. Switches Crypto group, tests fstream.binance.com WS push
4. Restores original node when done
"""

import asyncio
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CONTROLLER = "http://127.0.0.1:55379"
SECRET = "c98205f8-ea3a-462c-8dff-f3e8e1b8722b"
GROUP_NAME = "Crypto"

BLOCKED_REGIONS = {"hong kong", "japan", "usa", "singapore", "korea"}

TIMEOUT_SECONDS = 12


def clash_request(method: str, path: str, body: dict | None = None) -> dict | None:
    url = f"{CONTROLLER}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {SECRET}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"  Clash API error {e.code}: {path}")
        return None
    except Exception as e:
        print(f"  Clash API failed: {e}")
        return None


def get_group_info() -> tuple[str, list[str]]:
    data = clash_request("GET", f"/proxies/{urllib.parse.quote(GROUP_NAME)}")
    if not data:
        print("Failed to get Crypto group info")
        sys.exit(1)
    now = data.get("now", "")
    members = data.get("all", [])
    return now, members


def switch_node(name: str) -> bool:
    result = clash_request("PUT", f"/proxies/{urllib.parse.quote(GROUP_NAME)}", {"name": name})
    return result is not None


def is_blocked_region(name: str) -> bool:
    lower = name.lower()
    for region in BLOCKED_REGIONS:
        if region in lower:
            return True
    return False


def pick_candidates(members: list[str]) -> list[str]:
    seen_regions: set[str] = set()
    candidates: list[str] = []
    for name in members:
        if is_blocked_region(name):
            continue
        # Extract region: strip emoji prefix and trailing number/premium tag
        clean = name
        for ch in name:
            if ch.isascii() and ch.isalpha():
                clean = name[name.index(ch):]
                break
        region = ""
        for part in clean.split():
            if part[0].isdigit() or part.startswith("["):
                break
            region += part + " "
        region = region.strip().lower()
        if not region or region in seen_regions:
            continue
        seen_regions.add(region)
        candidates.append(name)
    return candidates


async def test_ws_push() -> bool:
    try:
        import websockets
    except ImportError:
        print("  websockets not installed")
        return False

    url = "wss://fstream.binance.com/ws"
    try:
        async with websockets.connect(url, open_timeout=8, close_timeout=2) as ws:
            await ws.send(json.dumps({
                "method": "SUBSCRIBE",
                "params": ["btcusdt@aggTrade"],
                "id": 1,
            }))
            # Wait for subscription ack
            ack = await asyncio.wait_for(ws.recv(), timeout=5)
            ack_data = json.loads(ack)
            if ack_data.get("result") is not None:
                return False
            # Now wait for actual market data
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT_SECONDS)
                data = json.loads(msg)
                if data.get("e") == "aggTrade" or (data.get("data", {}).get("e") == "aggTrade"):
                    return True
                # Could be another message, try once more
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(msg)
                return data.get("e") == "aggTrade" or (data.get("data", {}).get("e") == "aggTrade")
            except (asyncio.TimeoutError, TimeoutError):
                return False
    except (asyncio.TimeoutError, TimeoutError):
        return False
    except Exception as e:
        print(f"  WS error: {e}")
        return False


def main():
    print("=" * 60)
    print("Binance Futures WS Node Probe")
    print("=" * 60)

    original_node, members = get_group_info()
    print(f"Current node: {original_node}")
    print(f"Total members: {len(members)}")

    candidates = pick_candidates(members)
    print(f"Candidates to test (1 per region, skip HK/JP/SG/US/KR): {len(candidates)}")
    print()

    results_ok: list[str] = []
    results_fail: list[str] = []

    for i, name in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {name} ... ", end="", flush=True)

        if not switch_node(name):
            print("SWITCH FAILED")
            results_fail.append(name)
            continue

        time.sleep(1.5)  # Let connection settle

        ok = asyncio.run(test_ws_push())
        if ok:
            print("OK - data received")
            results_ok.append(name)
        else:
            print("BLOCKED - no data")
            results_fail.append(name)

    # Restore original
    print()
    print(f"Restoring original node: {original_node}")
    switch_node(original_node)

    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    if results_ok:
        print(f"\n  CAN receive Binance Futures WS ({len(results_ok)}):")
        for name in results_ok:
            print(f"    {name}")
    else:
        print("\n  NO working nodes found!")

    if results_fail:
        print(f"\n  BLOCKED ({len(results_fail)}):")
        for name in results_fail:
            print(f"    {name}")

    print()
    if results_ok:
        print(f"Recommendation: switch Crypto group to \"{results_ok[0]}\"")
        print("Then restart the monitor or wait 15min for auto-retry.")
    else:
        print("All tested nodes blocked. Binance REST fallback will continue working.")


if __name__ == "__main__":
    main()
