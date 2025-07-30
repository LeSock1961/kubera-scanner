import json, threading, time
from collections import deque, defaultdict
from datetime import datetime, timezone
from flask import Flask, jsonify
import websocket
import requests

app = Flask(__name__)
symbol_memory = defaultdict(lambda: deque(maxlen=1800))  # 30 min of 1s ticks
range_low_map = {}
bounce_map = {}
cooldown_map = {}
decay_cache = {}

def utc_now(): return datetime.now(timezone.utc).isoformat()

def on_message(ws, message):
    data = json.loads(message)
    if "data" not in data or "s" not in data["data"]:
        return
    symbol = data["data"]["s"].lower()
    if not symbol.endswith("usdt") or "perp" in symbol:
        return
    try:
        price = float(data["data"]["c"])
        symbol_memory[symbol].append((time.time(), price))
    except:
        pass

def start_websocket():
    url = "wss://stream.binance.com:9443/ws/!ticker@arr"
    ws = websocket.WebSocketApp(
        url, on_message=on_message, on_error=lambda *a: None, on_close=lambda *a: None)
    threading.Thread(target=ws.run_forever, daemon=True).start()

start_websocket()

def estimate_rsi(prices):
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i-1]
        if delta >= 0: gains.append(delta)
        else: losses.append(-delta)
    avg_gain = sum(gains[-14:]) / 14 if gains else 0.00001
    avg_loss = sum(losses[-14:]) / 14 if losses else 0.00001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def detect_decay(symbol):
    memory = list(symbol_memory[symbol])
    if len(memory) < 180:
        return None  # not enough data

    prices = [p for _, p in memory[-300:]]  # last 5m
    now_price = prices[-1]
    high, low = max(prices), min(prices)
    vol_range = (high - low) / low * 100

    if vol_range > 0.8:
        return None

    rsi = estimate_rsi(prices)
    if not (47 <= rsi <= 53):
        return None

    # wick rejection near range low
    wick_hits = sum(1 for p in prices if abs(p - low)/low < 0.001)
    if wick_hits < 3:
        return None

    decay_score = round(1.0 - (vol_range / 0.8), 3)  # closer to 1 = flatter
    range_low_map[symbol] = low
    return {
        "symbol": symbol.upper(),
        "decay_score": decay_score,
        "range_low": low,
        "last_price": now_price
    }

def detect_bounce(symbol):
    if symbol not in range_low_map:
        return False
    price = symbol_memory[symbol][-1][1]
    low = range_low_map[symbol]
    if price >= low * 1.0005:
        t_now = time.time()
        if bounce_map.get(symbol) is None or t_now - bounce_map[symbol] > 3:
            bounce_map[symbol] = t_now
            return True
    return False

@app.route("/range_candidates")
def range_candidates():
    results = []
    t_now = time.time()

    for symbol in symbol_memory:
        # Cooldown filter
        if cooldown_map.get(symbol, 0) > t_now:
            continue

        decay = decay_cache.get(symbol)
        if decay is None or t_now - decay.get("timestamp", 0) > 5:
            d = detect_decay(symbol)
            if d:
                d["timestamp"] = t_now
                decay_cache[symbol] = d
            else:
                decay_cache[symbol] = {"timestamp": t_now}
                continue

        d = decay_cache[symbol]
        if "symbol" not in d:
            continue

        ready = detect_bounce(symbol)
        if ready:
            cooldown_map[symbol] = t_now + 15  # 15s cooldown per coin

        results.append({
            "symbol": d["symbol"],
            "decay_score": d["decay_score"],
            "range_low": round(d["range_low"], 8),
            "last_price": round(d["last_price"], 8),
            "bounce_ready": ready,
            "timestamp": utc_now()
        })

    return jsonify(sorted(results, key=lambda x: -x["decay_score"])[:10])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
