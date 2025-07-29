# scanner_api.py

import asyncio
import json
from collections import deque, defaultdict
from datetime import datetime, timezone

import websockets
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import aiohttp

# ---------------- Config ---------------- #
MIN_PRICE = 0.002
MIN_VOLUME = 3_000_000
MEMORY_SECONDS = 3600
MIN_MEMORY_REQUIRED = 60  # Minimum 60 seconds of memory
SCAN_INTERVAL = 1  # seconds
price_memory = defaultdict(lambda: deque(maxlen=MEMORY_SECONDS))
coin_meta = {}  # symbol → {"volume": float, "price": float}
top_10_snapshot = {"timestamp": "", "top_10": []}

# ---------------- FastAPI App ---------------- #
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # You may restrict this later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def now():
    return datetime.now(timezone.utc)

def pct_change(old, new):
    return ((new - old) / old) * 100 if old else 0

def score_consistency(symbol):
    mem = list(price_memory[symbol])[-15:]
    if len(mem) < 5: return 0
    ups = sum(1 for i in range(1, len(mem)) if mem[i][1] > mem[i-1][1])
    up_ratio = ups / (len(mem) - 1)
    prices = [p for _, p in mem]
    drawdown = max(prices) - min(prices)
    penalty = drawdown / prices[-1] if prices[-1] > 0 else 0
    return round((up_ratio * 2.0) - penalty, 3)

async def preload_meta():
    url = "https://api.binance.com/api/v3/ticker/24hr"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            for d in data:
                sym = d["symbol"].lower()
                if sym.endswith("usdt"):
                    coin_meta[sym] = {
                        "volume": float(d.get("quoteVolume", 0)),
                        "price": float(d.get("lastPrice", 0))
                    }

async def scanner_loop():
    global top_10_snapshot
    await preload_meta()
    uri = "wss://stream.binance.com:9443/ws/!miniTicker@arr"

    async with websockets.connect(uri) as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                tick_data = json.loads(raw)
                timestamp = now()
                candidates = []

                for item in tick_data:
                    symbol = item["s"].lower()
                    if not symbol.endswith("usdt"): continue
                    price = float(item["c"])
                    if price < MIN_PRICE: continue

                    meta = coin_meta.get(symbol, {})
                    vol = meta.get("volume", 0)
                    if vol < MIN_VOLUME: continue

                    memory = price_memory[symbol]
                    memory.append((timestamp, price))
                    if len(memory) < MIN_MEMORY_REQUIRED:
                        continue

                    price_now = memory[-1][1]
                    price_past = memory[0][1]
                    change_1h = pct_change(price_past, price_now)
                    velocity_1s = pct_change(memory[-2][1], price_now) if len(memory) > 2 else 0
                    movement_score = change_1h + (velocity_1s * 5)
                    consistency = score_consistency(symbol)
                    total_score = round(movement_score + consistency, 3)

                    candidates.append({
                        "symbol": symbol,
                        "price": round(price, 6),
                        "change_1h": round(change_1h, 3),
                        "velocity": round(velocity_1s, 3),
                        "consistency": consistency,
                        "score": total_score
                    })

                top_10 = sorted(candidates, key=lambda x: x["score"], reverse=True)[:10]
                top_10_snapshot = {
                    "timestamp": now().isoformat(),
                    "top_10": top_10
                }

                await asyncio.sleep(SCAN_INTERVAL)

            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                print("WebSocket disconnected. Reconnecting...")
                return await scanner_loop()

# ---------------- FastAPI Endpoints ---------------- #
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scanner_loop())

@app.get("/top10")
def get_top_10():
    return top_10_snapshot

# ---------------- Main Entry ---------------- #
if __name__ == "__main__":
    uvicorn.run("scanner_api:app", host="0.0.0.0", port=8000)
