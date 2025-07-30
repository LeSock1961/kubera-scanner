import json, threading, time, os
from collections import deque, defaultdict
from datetime import datetime, timezone
from flask import Flask, jsonify
import websocket

app = Flask(__name__)

symbol_memory = defaultdict(lambda: deque(maxlen=1800))  # 30m of 1s data
decay_state = {}            # symbol → tier, score, bounce
range_low_map = {}
bounce_timer = {}
lock = threading.Lock()
DECAY_STATE_PATH = "/tmp/decay_state.json"

def utc_now(): return datetime.now(timezone.utc).isoformat()

# === Load + Save State ===
def load_saved_decay():
    global decay_state
    try:
        if os.path.exists(DECAY_STATE_PATH):
            with open(DECAY_STATE_PATH, "r") as f:
                decay_state.update(json.load(f))
            print(f"✅ decay_state loaded from disk: {len(decay_state)} symbols")
    except Exception as e:
        print("load_saved_decay ERROR:", e)

def save_decay_state():
    try:
        with lock:
            with open(DECAY_STATE_PATH, "w") as f:
                json.dump(decay_state, f)
    except Exception as e:
        print("save_decay_state ERROR:", e)

def auto_save_loop():
    while True:
        save_decay_state()
        time.sleep(5)

# === WebSocket Feed ===
def on_message(ws, message):
    try:
        tickers = json.loads(message)
        for entry in tickers:
            symbol = entry["s"].lower()
            if not symbol.endswith("usdt") or "perp" in symbol:
                continue
            price = float(entry["c"])
            symbol_memory[symbol].append((time.time(), price))
    except: pass

def start_websocket():
    url = "wss://stream.binance.com:9443/ws/!ticker@arr"
    ws = websocket.WebSocketApp(
        url, on_message=on_message,
        on_error=lambda *a: None, on_close=lambda *a: None
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# === RSI + Decay Evaluator ===
def estimate_rsi(prices):
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i-1]
        (gains if delta >= 0 else losses).append(abs(delta))
    avg_gain = sum(gains[-14:]) / 14 if gains else 0.00001
    avg_loss = sum(losses[-14:]) / 14 if losses else 0.00001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def evaluate_symbol(symbol):
    memory = list(symbol_memory[symbol])
    if len(memory) < 180:
        return None

    # Movement pre-filter
    recent = memory[-10:] if len(memory) >= 10 else memory
    prices = [p for _, p in recent]
    if len(prices) < 2:
        return None
    low10, high10 = min(prices), max(prices)
    if low10 == 0 or ((high10 - low10) / low10 * 100) < 0.1:
        return None

    prices = [p for _, p in memory[-300:]]
    now_price = prices[-1]
    high, low = max(prices), min(prices)
    vol = (high - low) / low * 100
    if vol > 0.8: return None
    rsi = estimate_rsi(prices)
    if not (48 <= rsi <= 52): return None
    wick_hits = sum(1 for p in prices if abs(p - low)/low < 0.001)
    if wick_hits < 3: return None

    decay_score = round(1.0 - (vol / 0.8), 3)
    range_low_map[symbol] = low

    bounce = False
    if now_price >= low * 1.0001:
        t_now = time.time()
        last = bounce_timer.get(symbol, 0)
        if t_now - last > 3:
            bounce_timer[symbol] = t_now
            bounce = True

    tier = 1 if bounce else 2 if decay_score > 0.85 else 3 if decay_score > 0.75 else None
    if tier is None: return None

    return {
        "symbol": symbol.upper(),
        "decay_score": decay_score,
        "range_low": round(low, 8),
        "last_price": round(now_price, 8),
        "bounce_ready": bounce,
        "tier": tier,
        "timestamp": utc_now()
    }

# === Continuous Decay Scan ===
def decay_loop():
    while True:
        with lock:
            for symbol in list(symbol_memory.keys()):
                result = evaluate_symbol(symbol)
                if result:
                    decay_state[symbol] = result
                elif symbol in decay_state:
                    del decay_state[symbol]
        time.sleep(1)

# === API Endpoints ===
@app.route("/range_candidates")
def range_candidates():
    with lock:
        return jsonify(
            sorted(list(decay_state.values()), key=lambda x: (-x["tier"], -x["decay_score"]))[:10]
        )

@app.route("/status")
def status():
    with lock:
        t1 = sum(1 for v in decay_state.values() if v["tier"] == 1)
        t2 = sum(1 for v in decay_state.values() if v["tier"] == 2)
        t3 = sum(1 for v in decay_state.values() if v["tier"] == 3)
        return jsonify({
            "symbols_loaded": len(symbol_memory),
            "symbols_active": len(decay_state),
            "tier_1": t1,
            "tier_2": t2,
            "tier_3": t3,
            "timestamp": utc_now()
        })

# === Launch ===
if __name__ == "__main__":
    load_saved_decay()
    start_websocket()
    threading.Thread(target=decay_loop, daemon=True).start()
    threading.Thread(target=auto_save_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
