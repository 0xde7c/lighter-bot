#!/usr/bin/env python3
"""
RSI Bot v8.0 — RSI + Whale Flow Strategy
  1. 1m RSI for entry timing (buy zone 35-50, sell zone 50-65)
  2. 5m RSI for trend direction (above 48 = longs, below 52 = shorts)
  3. Whale flow confirmation on entry + protection in position
  4. RSI reversal exit (primary), trailing stop (secondary), hard stop (backstop)
  5. No fixed TP — let RSI + trail decide when to exit
  6. No imbalance in entry/exit — clean RSI signal only
  Safety: SL always on exchange, position sync, no naked positions, no double orders
"""
import asyncio, time, requests, lighter, threading, json, collections, os, traceback, urllib.request
from datetime import datetime
import websocket as ws_lib
from lighter.signer_client import CreateOrderTxReq

# ══════════════════════════════════════════════════════════════════════════
# MODE
# ══════════════════════════════════════════════════════════════════════════
PAPER_TRADE = False
PAPER_STARTING_BALANCE = 71.30

# ── CONFIG ───────────────────────────────────────────────────────────────
ACCOUNT_INDEX=716892; API_KEY_INDEX=3
API_PRIVATE_KEY="5a9634234bac159b30afd8f6bca2a4fa57a46e6318a37447efd364ee90756d2138da41588bdabe55"
LIGHTER_URL="https://mainnet.zklighter.elliot.ai"; MARKET_INDEX=1
TG_TOKEN="8536956116:AAGOWByFex_n1wraFuGFwf7e5YPVe2Vyegw"; TG_CHAT="5583279698"

BASE_AMOUNT = 150  # 0.0015 BTC (~$100 notional, ~$2 margin at 50x)

# ── RSI STRATEGY ─────────────────────────────────────────────────────────
RSI_PERIOD = 14

# Entry zones (1m RSI) — wide zones, delta is the real filter
RSI_LONG_ZONE_LOW = 15      # allow longs from deeply oversold
RSI_LONG_ZONE_HIGH = 50     # don't long above midline
RSI_SHORT_ZONE_LOW = 50     # don't short below midline
RSI_SHORT_ZONE_HIGH = 85    # allow shorts from deeply overbought
RSI_MIN_DELTA = 3.5         # min live RSI change over lookback — filters noise
RSI_DELTA_LOOKBACK = 15     # seconds to look back for RSI direction
MIN_PRICE_MOVE_PCT = 0.05   # min 0.05% price move in 60s to confirm momentum

# 5m RSI direction filter — only block extreme contradictions
RSI_5M_LONG_MIN = 25        # only block longs in extreme 5m crash
RSI_5M_SHORT_MAX = 75       # only block shorts in extreme 5m pump

# RSI exit levels
RSI_LONG_PROFIT_EXIT = 65   # let longs run further before profit-taking
RSI_SHORT_PROFIT_EXIT = 35  # let shorts run further
RSI_LONG_FAIL = 20          # wide fail — don't kill longs entered at RSI 25
RSI_SHORT_FAIL = 80         # wide fail — don't kill shorts entered at RSI 75
RSI_STALL_MIN_HOLD = 60     # detect stalls sooner (was 120)
RSI_STALL_MIN_PROFIT = 0.0002  # lower bar for stall profit-take

# ── EXITS ────────────────────────────────────────────────────────────────
SL_PCT = 0.0020             # 0.20% SL (~$0.20 risk at $100 notional)
HARD_STOP_PCT = 0.0025      # 0.25% hard stop backstop
TRAIL_ACTIVATE_PCT = 0.0012 # 0.12% to activate trail
TRAIL_DISTANCE_PCT = 0.0012 # 0.12% trail distance (wide — let winners breathe)
TRAIL_MID_ACTIVATE = 0.0025 # 0.25%+ profit → mid tier
TRAIL_MID_DISTANCE = 0.0008 # 0.08% mid trail distance
TRAIL_TIGHT_ACTIVATE = 0.0040 # 0.40%+ profit → lock it in
TRAIL_TIGHT_DISTANCE = 0.0005 # 0.05% tight trail
BREAKEVEN_ACTIVATE_PCT = 999.0   # DISABLED — trail handles profit protection
EARLY_STALL_SECS = 30       # cut stalling trades after 30s
EARLY_STALL_MIN_PROFIT = 0.0003  # must be at least +0.03% by then
LOSER_TIME_CUT_SECS = 120       # close if still red after 120s
MAX_HOLD_SECS = 300         # 5 min normal
TRAIL_MAX_HOLD_SECS = 420   # 7 min trailing
LOCK_DURATION = 450

# ── WHALE FLOW ───────────────────────────────────────────────────────────
WHALE_MIN_BTC = 2.0         # min BTC for whale classification
WHALE_CONFIRM_WINDOW = 15   # seconds to look back for confirmation
WHALE_BLOCK_WINDOW = 10     # skip entry if whale against in last N secs
WHALE_TIGHTEN_TRAIL_PCT = 0.0003  # tighten trail to 0.03% on whale against

# ── COOLDOWNS ────────────────────────────────────────────────────────────
MIN_TRADE_INTERVAL = 5      # back-to-back trading (was 15)
LOSS_COOLDOWN_SECS = 10     # brief pause after loss (was 30)
LOSS_STREAK_COOLDOWN = 30   # after 3 losses (was 60)
MAX_TRADES_HOUR = 60
DAILY_LOSS_LIMIT = 3.0      # ~10.5% of $28 account (was 2.0)

# ── KEPT FOR LOGGING ─────────────────────────────────────────────────────
IMB_LEVELS = 5; IMB_EMA_ALPHA = 0.1
SMOOTH_TICKS = 10; LOOKBACK_TICKS = 30

EXIT_SETTLE_SECS = 5

STATE_FILE="/root/rsi_bot/.bot_state.json"
LOCK_FILE="/root/rsi_bot/.entry_lock"
LOCAL_POS_FILE="/root/rsi_bot/.local_position.json"

# ── GLOBAL STATE ─────────────────────────────────────────────────────────
tick_prices=collections.deque(maxlen=1200); tick_lock=threading.Lock()
live_rsi_history=collections.deque(maxlen=120)  # (time, rsi) tuples, ~2min of data
ob_bids=[]; ob_asks=[]; ob_lock=threading.Lock()
imb_ema=0.0; imb_ema_lock=threading.Lock()

acct_ws_lock = threading.Lock()
acct_ws_position = {"size": 0.0, "sign": 0, "entry": 0.0, "open_orders": 0}
acct_ws_balance = 0.0
acct_ws_last_trade = None
acct_ws_ready = threading.Event()
acct_ws_trade_queue = collections.deque(maxlen=50)

tape_lock = threading.Lock()
tape_recent = collections.deque(maxlen=500)
tape_whale_threshold = 0.5
tape_last_whale = None
_hard_stop_tick_count = 0  # consecutive ticks beyond hard stop

# Candle state for RSI
candles_1m = collections.deque(maxlen=120)  # 2 hours of 1m candles
candles_5m = collections.deque(maxlen=120)  # 10 hours of 5m candles
_candle_1m = {"open": 0, "high": 0, "low": 0, "close": 0, "time": 0, "minute": 0}
_candle_5m = {"open": 0, "high": 0, "low": 0, "close": 0, "time": 0, "slot": 0}

bot_start_time = time.time()


# ══════════════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════════════
def api_get_position():
    try:
        d = requests.get(f"{LIGHTER_URL}/api/v1/account?by=index&value={ACCOUNT_INDEX}", timeout=10).json()
        acct = d["accounts"][0]
        bal = float(acct["available_balance"])
        positions = acct.get("positions", [])
        if not positions:
            return {"size": 0, "entry": 0, "sign": 0, "open_orders": 0, "balance": bal}
        pos = positions[0]
        size = abs(float(pos.get("position", 0)))
        entry = float(pos.get("avg_entry_price", 0))
        api_sign = int(pos.get("sign", 0))  # use API's sign field: 1=long, -1=short
        raw_pos = float(pos.get("position", 0))
        if api_sign != 0:
            sign = 1 if api_sign > 0 else -1
        elif raw_pos != 0:
            sign = 1 if raw_pos > 0 else -1
        else:
            sign = 0
        oo = int(pos.get("open_order_count", 0))
        return {"size": size, "entry": entry, "sign": sign, "open_orders": oo, "balance": bal}
    except Exception as e:
        print(f"  API error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# LOCAL POSITION TRACKER
# ══════════════════════════════════════════════════════════════════════════
class LocalPosition:
    def __init__(self):
        self.in_position = False
        self.side = None
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.size = 0.0
        self.sl_price = 0.0
        self.tp_price = 0.0
        self.oco_attached = False
        self._load()

    def _load(self):
        try:
            with open(LOCAL_POS_FILE) as f:
                d = json.load(f)
                for k in ['in_position','side','entry_price','entry_time','size',
                          'sl_price','tp_price','oco_attached']:
                    if k in d: setattr(self, k, d[k])
                if self.in_position:
                    print(f"  Loaded: {self.side} @ ${self.entry_price:,.1f} oco={self.oco_attached}")
        except: pass

    def _save(self):
        try:
            with open(LOCAL_POS_FILE, "w") as f:
                json.dump({k: getattr(self, k) for k in
                    ['in_position','side','entry_price','entry_time','size',
                     'sl_price','tp_price','oco_attached']}, f)
        except Exception as e:
            print(f"  Save error: {e}")

    def open(self, side, entry_price, size=0.005):
        self.in_position = True
        self.side = side
        self.entry_price = entry_price
        self.entry_time = time.time()
        self.size = size
        self.oco_attached = False
        self.trailing_active = False
        self.best_price = entry_price
        self.trail_stop_price = 0.0
        self.rsi_at_entry = 0.0  # set by caller
        if side == 'long':
            self.sl_price = entry_price * (1 - SL_PCT)
            self.tp_price = entry_price * (1 + 0.005)  # 0.50% — effectively no TP, RSI handles exit
        else:
            self.sl_price = entry_price * (1 + SL_PCT)
            self.tp_price = entry_price * (1 - 0.005)
        self._save()
        print(f"  OPENED {side} @ ${entry_price:,.1f} SL=${self.sl_price:,.1f}")

    def close(self, reason=""):
        side = self.side; entry = self.entry_price
        self.in_position = False; self.side = None; self.entry_price = 0.0
        self.entry_time = 0.0; self.size = 0.0; self.sl_price = 0.0
        self.tp_price = 0.0; self.oco_attached = False
        self._save()
        print(f"  CLOSED {side} from ${entry:,.1f} ({reason})")

    def unrealized_pct(self, current_price):
        if not self.in_position: return 0.0
        if self.side == 'long':
            return (current_price - self.entry_price) / self.entry_price
        return (self.entry_price - current_price) / self.entry_price

    def hold_time(self):
        return int(time.time() - self.entry_time) if self.in_position else 0

    def check_sl(self, current_price):
        """Check SL only — no fixed TP, RSI handles exits."""
        if not self.in_position: return False
        if self.side == 'long' and current_price <= self.sl_price: return True
        if self.side == 'short' and current_price >= self.sl_price: return True
        return False

    def update_trailing_stop(self, current_price, distance_override=None):
        """Update trailing stop. Returns 'trail_exit' if triggered."""
        if not self.in_position: return None
        profit_pct = self.unrealized_pct(current_price)

        # Progressive 3-tier trail: wider early, tighten as profit grows
        if distance_override:
            trail_dist = distance_override
        elif profit_pct >= TRAIL_TIGHT_ACTIVATE:
            trail_dist = TRAIL_TIGHT_DISTANCE   # 0.05% at 0.40%+
        elif profit_pct >= TRAIL_MID_ACTIVATE:
            trail_dist = TRAIL_MID_DISTANCE     # 0.08% at 0.25%+
        else:
            trail_dist = TRAIL_DISTANCE_PCT     # 0.12% at 0.12%+

        if not self.trailing_active and profit_pct >= TRAIL_ACTIVATE_PCT:
            self.trailing_active = True
            self.best_price = current_price
            if self.side == 'long':
                self.trail_stop_price = current_price * (1 - trail_dist)
            else:
                self.trail_stop_price = current_price * (1 + trail_dist)
            print(f"  TRAIL ACTIVATED at +{profit_pct*100:.3f}% | stop=${self.trail_stop_price:,.1f}")
            return None

        if not self.trailing_active: return None

        if self.side == 'long':
            if current_price > self.best_price:
                self.best_price = current_price
                self.trail_stop_price = current_price * (1 - trail_dist)
            if current_price <= self.trail_stop_price:
                return 'trail_exit'
        else:
            if current_price < self.best_price:
                self.best_price = current_price
                self.trail_stop_price = current_price * (1 + trail_dist)
            if current_price >= self.trail_stop_price:
                return 'trail_exit'
        return None


# ══════════════════════════════════════════════════════════════════════════
# PAPER TRADE ENGINE
# ══════════════════════════════════════════════════════════════════════════
class PaperTrader:
    def __init__(self, starting_balance):
        self.balance = starting_balance
        self.total_trades = 0; self.wins = 0; self.losses = 0
        self.daily_pnl = 0.0; self.trade_log = []

    def execute_entry(self, side, price):
        slip = price * 0.0001
        return price + slip if side == 'long' else price - slip

    def execute_exit(self, side, entry_price, exit_price, reason):
        slip = exit_price * 0.0001
        if side == 'long':
            fill = exit_price - slip; pnl_pct = (fill - entry_price) / entry_price
        else:
            fill = exit_price + slip; pnl_pct = (entry_price - fill) / entry_price
        pnl_usd = pnl_pct * BASE_AMOUNT
        self.balance += pnl_usd; self.daily_pnl += pnl_usd; self.total_trades += 1
        if pnl_usd >= 0: self.wins += 1
        else: self.losses += 1
        self.trade_log.append({"time": time.strftime("%H:%M:%S"), "side": side,
            "entry": entry_price, "exit": fill, "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd, "reason": reason, "balance": self.balance})
        return pnl_usd, pnl_pct, fill

    def summary(self):
        wr = f"{self.wins/(self.wins+self.losses)*100:.0f}%" if self.wins+self.losses > 0 else "N/A"
        return f"Bal: ${self.balance:.2f} | Day: ${self.daily_pnl:.2f} | {self.wins}W/{self.losses}L ({wr})"


# ── ENTRY LOCK ──────────────────────────────────────────────────────────
def lock_entry():
    with open(LOCK_FILE, "w") as f: f.write(str(time.time()))

def unlock_entry():
    try: os.remove(LOCK_FILE)
    except: pass

def is_entry_locked():
    try:
        with open(LOCK_FILE) as f: lock_time = float(f.read().strip())
        if time.time() - lock_time < LOCK_DURATION: return True
        unlock_entry(); return False
    except: return False


# ══════════════════════════════════════════════════════════════════════════
# WS FEED (unchanged — orderbook, account, trade tape)
# ══════════════════════════════════════════════════════════════════════════
def ws_thread():
    global ob_bids, ob_asks, imb_ema
    global acct_ws_position, acct_ws_balance, acct_ws_last_trade
    global tape_last_whale

    def on_msg(ws, msg):
        global ob_bids, ob_asks, imb_ema
        global acct_ws_position, acct_ws_balance, acct_ws_last_trade
        global tape_last_whale
        try:
            data = json.loads(msg)
            msg_type = data.get("type", "")
            channel = data.get("channel", "")

            if "order_book" in channel:
                ob = data.get("order_book", {})
                raw_asks = ob.get("asks", []); raw_bids = ob.get("bids", [])
                asks_parsed = [(float(a["price"]), float(a.get("size", "0"))) for a in raw_asks if float(a.get("size", "0")) > 0]
                bids_parsed = [(float(b["price"]), float(b.get("size", "0"))) for b in raw_bids if float(b.get("size", "0")) > 0]
                if asks_parsed and bids_parsed:
                    best_ask = min(a[0] for a in asks_parsed)
                    best_bid = max(b[0] for b in bids_parsed)
                    mid = (best_ask + best_bid) / 2
                    with tick_lock: tick_prices.append((time.time(), mid))
                    bids_sorted = sorted(bids_parsed, key=lambda x: -x[0])
                    asks_sorted = sorted(asks_parsed, key=lambda x: x[0])
                    with ob_lock: ob_bids[:] = bids_sorted; ob_asks[:] = asks_sorted
                    bid_depth = sum(s for _, s in bids_sorted[:IMB_LEVELS])
                    ask_depth = sum(s for _, s in asks_sorted[:IMB_LEVELS])
                    total = bid_depth + ask_depth
                    raw_imb = (bid_depth - ask_depth) / total if total > 0 else 0
                    with imb_ema_lock: imb_ema = IMB_EMA_ALPHA * raw_imb + (1 - IMB_EMA_ALPHA) * imb_ema

            elif "account_all" in channel or msg_type in ("update/account_all", "update/account"):
                with acct_ws_lock:
                    positions = data.get("positions", {})
                    pos_data = positions.get(str(MARKET_INDEX)) or positions.get("1")
                    if pos_data:
                        if isinstance(pos_data, list):
                            pos_data = pos_data[0] if pos_data else None
                        if pos_data:
                            size = abs(float(pos_data.get("position", "0")))
                            sign = int(pos_data.get("sign", 0))
                            entry = float(pos_data.get("avg_entry_price", "0"))
                            oo = int(pos_data.get("open_order_count", 0))
                            acct_ws_position = {"size": size, "sign": sign, "entry": entry, "open_orders": oo}
                        else:
                            acct_ws_position = {"size": 0.0, "sign": 0, "entry": 0.0, "open_orders": 0}
                    assets = data.get("assets", {})
                    usdc = assets.get("3") or assets.get("0")
                    if usdc:
                        acct_ws_balance = float(usdc.get("balance", "0"))
                    trades = data.get("trades", {})
                    market_trades = trades.get(str(MARKET_INDEX)) or trades.get("1")
                    if market_trades:
                        if isinstance(market_trades, dict):
                            market_trades = [market_trades]
                        for t in market_trades:
                            trade_info = {
                                "price": float(t.get("price", "0")),
                                "size": float(t.get("size", "0")),
                                "side": t.get("type", ""),
                                "trade_id": t.get("trade_id", 0),
                                "time": time.time(),
                                "is_our_trade": (
                                    t.get("ask_account_id") == ACCOUNT_INDEX or
                                    t.get("bid_account_id") == ACCOUNT_INDEX
                                )
                            }
                            if trade_info["is_our_trade"]:
                                acct_ws_last_trade = trade_info
                                acct_ws_trade_queue.append(trade_info)
                                print(f"  WS Trade: {trade_info['side']} {trade_info['size']:.5f} @ ${trade_info['price']:,.1f}")
                    acct_ws_ready.set()

            elif "trade:" in channel and "account" not in channel:
                tape_trades = data.get("trades", [])
                if isinstance(tape_trades, dict):
                    tape_trades = [tape_trades]
                with tape_lock:
                    for t in tape_trades:
                        size = float(t.get("size", "0"))
                        price = float(t.get("price", "0"))
                        side = t.get("type", "")
                        trade_info = {
                            "price": price, "size": size, "side": side,
                            "time": time.time(),
                            "usd": float(t.get("usd_amount", "0")),
                            "ask_account": t.get("ask_account_id", 0),
                            "bid_account": t.get("bid_account_id", 0),
                        }
                        tape_recent.append(trade_info)
                        if size >= tape_whale_threshold:
                            tape_last_whale = trade_info
                            if size >= WHALE_MIN_BTC:
                                print(f"  WHALE {side.upper()} {size:.2f} BTC (${trade_info['usd']:,.0f}) @ ${price:,.1f}")
                        if (t.get("ask_account_id") == ACCOUNT_INDEX or
                            t.get("bid_account_id") == ACCOUNT_INDEX):
                            with acct_ws_lock:
                                acct_ws_last_trade = {
                                    "price": price, "size": size, "side": side,
                                    "trade_id": t.get("trade_id", 0),
                                    "time": time.time(), "is_our_trade": True
                                }
                                acct_ws_trade_queue.append(acct_ws_last_trade)
                                print(f"  OUR FILL: {side} {size:.5f} @ ${price:,.1f}")
        except: pass

    def on_close(ws, *a): time.sleep(2); run()
    def on_open(ws):
        print("  WS connected")
        ws.send(json.dumps({"type": "subscribe", "channel": "order_book/1"}))
        ws.send(json.dumps({"type": "subscribe", "channel": f"account_all/{ACCOUNT_INDEX}"}))
        ws.send(json.dumps({"type": "subscribe", "channel": "trade/1"}))
        print(f"  Subscribed to account_all/{ACCOUNT_INDEX} + trade/1")
    def run():
        try:
            ws_lib.WebSocketApp("wss://mainnet.zklighter.elliot.ai/stream",
                on_message=on_msg, on_close=on_close, on_open=on_open
            ).run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"  WS error: {e}"); time.sleep(2); run()
    run()


# ══════════════════════════════════════════════════════════════════════════
# CANDLE BUILDING + RSI COMPUTATION
# ══════════════════════════════════════════════════════════════════════════
def update_candles(price):
    """Called every main loop iteration. Builds 1m and 5m candles from price."""
    global _candle_1m, _candle_5m
    now = time.time()
    cur_min = int(now // 60)
    cur_5m = int(now // 300)

    # 1M candle
    if _candle_1m["minute"] == 0 or (_candle_1m["open"] == 0 and _candle_1m["minute"] == cur_min):
        _candle_1m = {"open": price, "high": price, "low": price, "close": price, "time": now, "minute": cur_min}
    elif cur_min != _candle_1m["minute"]:
        candles_1m.append({"open": _candle_1m["open"], "high": _candle_1m["high"],
            "low": _candle_1m["low"], "close": _candle_1m["close"], "time": _candle_1m["time"]})
        _candle_1m = {"open": price, "high": price, "low": price, "close": price, "time": now, "minute": cur_min}
    else:
        _candle_1m["high"] = max(_candle_1m["high"], price)
        _candle_1m["low"] = min(_candle_1m["low"], price)
        _candle_1m["close"] = price

    # 5M candle
    if _candle_5m["slot"] == 0 or (_candle_5m["open"] == 0 and _candle_5m["slot"] == cur_5m):
        _candle_5m = {"open": price, "high": price, "low": price, "close": price, "time": now, "slot": cur_5m}
    elif cur_5m != _candle_5m["slot"]:
        candles_5m.append({"open": _candle_5m["open"], "high": _candle_5m["high"],
            "low": _candle_5m["low"], "close": _candle_5m["close"], "time": _candle_5m["time"]})
        _candle_5m = {"open": price, "high": price, "low": price, "close": price, "time": now, "slot": cur_5m}
    else:
        _candle_5m["high"] = max(_candle_5m["high"], price)
        _candle_5m["low"] = min(_candle_5m["low"], price)
        _candle_5m["close"] = price


def compute_rsi(candles_list, period=14):
    """Compute RSI using Wilder's smoothing — matches TradingView RSI."""
    if len(candles_list) < period + 1:
        return None
    closes = [c["close"] for c in candles_list]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(0, d) for d in deltas]
    losses_list = [max(0, -d) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses_list[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses_list[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_rsi_1m():
    """RSI from closed 1m candles."""
    return compute_rsi(list(candles_1m), RSI_PERIOD)

def get_rsi_1m_live(price):
    """Live RSI including current incomplete candle."""
    candles = list(candles_1m)
    if _candle_1m["minute"] > 0:
        candles.append({"open": _candle_1m["open"], "high": _candle_1m["high"],
            "low": _candle_1m["low"], "close": price, "time": _candle_1m["time"]})
    return compute_rsi(candles, RSI_PERIOD)

def get_prev_rsi_1m():
    """RSI from one candle ago (for slope detection)."""
    candles = list(candles_1m)
    if len(candles) < RSI_PERIOD + 2: return None
    return compute_rsi(candles[:-1], RSI_PERIOD)

def get_rsi_5m():
    """RSI from closed 5m candles."""
    return compute_rsi(list(candles_5m), RSI_PERIOD)


def is_warmed_up():
    """Need at least RSI_PERIOD+2 closed 1m candles."""
    return len(candles_1m) >= RSI_PERIOD + 2


def bootstrap_candles():
    """Fetch historical 1m and 5m candles from Lighter API to skip warmup."""
    global _candle_1m, _candle_5m
    base = "https://mainnet.zklighter.elliot.ai/api/v1/candles"
    now = int(time.time())

    for resolution, deque_ref, mins_back, label in [
        ("1m", candles_1m, 30, "1m"),   # 30 candles — plenty for RSI 14
        ("5m", candles_5m, 120, "5m"),   # 24 candles of 5m data
    ]:
        try:
            start = now - (mins_back * 60)
            url = (f"{base}?market_id={MARKET_INDEX}&resolution={resolution}"
                   f"&start_timestamp={start}&end_timestamp={now}&count_back={mins_back}")
            resp = json.loads(urllib.request.urlopen(url, timeout=10).read().decode())
            if resp.get("code") != 200 or "c" not in resp:
                print(f"  Bootstrap {label}: API error {resp}")
                continue
            raw = resp["c"]
            # Don't include the last candle (it's still forming)
            closed = raw[:-1] if len(raw) > 1 else raw
            for c in closed:
                deque_ref.append({
                    "open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"],
                    "time": c["t"] / 1000.0  # API returns ms, we store seconds
                })
            print(f"  Bootstrap {label}: loaded {len(closed)} candles (need {RSI_PERIOD+2})")
        except Exception as e:
            print(f"  Bootstrap {label} failed: {e}")

    # Set current candle slots so update_candles() doesn't double-count
    cur_min = int(now // 60)
    cur_5m = int(now // 300)
    _candle_1m = {"open": 0, "high": 0, "low": 0, "close": 0, "time": 0, "minute": cur_min}
    _candle_5m = {"open": 0, "high": 0, "low": 0, "close": 0, "time": 0, "slot": cur_5m}

    warmed = is_warmed_up()
    rsi = compute_rsi(list(candles_1m), RSI_PERIOD)
    print(f"  Bootstrap done: warmed={warmed} 1m_candles={len(candles_1m)} 5m_candles={len(candles_5m)}"
          f" RSI_1m={rsi:.1f}" if rsi else f"  Bootstrap done: warmed={warmed} 1m={len(candles_1m)} 5m={len(candles_5m)}")


# ══════════════════════════════════════════════════════════════════════════
# WHALE FLOW HELPERS
# ══════════════════════════════════════════════════════════════════════════
def chop_filter_ok():
    """
    Directional efficiency filter — blocks entries in choppy markets.
    Samples price every 10s over 5 minutes, measures net vs total path.
    Returns True if market is trending enough to trade.
    """
    with tick_lock:
        ticks = list(tick_prices)
    if len(ticks) < 60:
        return False  # need at least 60 ticks (~60s) before allowing trades
    cutoff = time.time() - CHOP_WINDOW_SECS
    recent = [(t, p) for t, p in ticks if t > cutoff]
    if len(recent) < 10:
        return True
    # Sample every ~10 seconds to smooth tick noise
    sampled = []
    last_t = 0
    for t, p in recent:
        if t - last_t >= 10:
            sampled.append(p)
            last_t = t
    sampled.append(recent[-1][1])  # always include latest
    if len(sampled) < 5:
        return True
    net_move = abs(sampled[-1] - sampled[0])
    total_move = sum(abs(sampled[i] - sampled[i-1]) for i in range(1, len(sampled)))
    if total_move == 0:
        return True
    efficiency = net_move / total_move
    range_pct = (max(sampled) - min(sampled)) / sampled[0]
    ok = efficiency >= CHOP_MIN_EFFICIENCY and range_pct >= CHOP_MIN_RANGE_PCT / 100
    if not ok:
        print(f"    chop: eff={efficiency:.2f} range={range_pct*100:.3f}% samples={len(sampled)}")
    return ok


def get_recent_whales(seconds=15):
    """Get whale trades (>= WHALE_MIN_BTC) in last N seconds."""
    with tape_lock:
        cutoff = time.time() - seconds
        return [t for t in tape_recent if t["time"] > cutoff and t["size"] >= WHALE_MIN_BTC]

def whale_against_entry(direction):
    """Check if a whale traded against our intended entry direction recently."""
    whales = get_recent_whales(WHALE_BLOCK_WINDOW)
    if not whales: return False
    if direction == 'long':
        return any(w["side"] == "sell" for w in whales)
    else:
        return any(w["side"] == "buy" for w in whales)

def whale_confirms_entry(direction):
    """Check if whale flow confirms our direction."""
    whales = get_recent_whales(WHALE_CONFIRM_WINDOW)
    if not whales: return False
    if direction == 'long':
        return any(w["side"] == "buy" for w in whales)
    else:
        return any(w["side"] == "sell" for w in whales)

def whale_against_position(side):
    """Check if whale is trading against our open position (last 10s)."""
    whales = get_recent_whales(10)
    if not whales: return False, 0
    if side == 'long':
        sells = [w for w in whales if w["side"] == "sell"]
        if sells:
            biggest = max(sells, key=lambda w: w["size"])
            return True, biggest["size"]
    else:
        buys = [w for w in whales if w["side"] == "buy"]
        if buys:
            biggest = max(buys, key=lambda w: w["size"])
            return True, biggest["size"]
    return False, 0


# ══════════════════════════════════════════════════════════════════════════
# SIGNAL HELPERS (kept for logging)
# ══════════════════════════════════════════════════════════════════════════
def get_imb_ema():
    with imb_ema_lock: return imb_ema

def get_momentum():
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < LOOKBACK_TICKS + SMOOTH_TICKS: return None, None
    current = sum(t[1] for t in ticks[-SMOOTH_TICKS:]) / SMOOTH_TICKS
    pe = len(ticks) - LOOKBACK_TICKS; ps = max(0, pe - SMOOTH_TICKS)
    past = sum(t[1] for t in ticks[ps:pe]) / (pe - ps)
    mom = (current - past) / past
    return current, mom

def tape_get_recent_bias(seconds=5):
    with tape_lock:
        cutoff = time.time() - seconds
        recent = [t for t in tape_recent if t["time"] > cutoff]
        if not recent: return 0.0
        buy_vol = sum(t["size"] for t in recent if t["side"] == "buy")
        sell_vol = sum(t["size"] for t in recent if t["side"] == "sell")
        total = buy_vol + sell_vol
        return (buy_vol - sell_vol) / total if total > 0 else 0.0


# ── PNL CALCULATION ─────────────────────────────────────────────────────
def calc_pnl_from_prices(side, entry_price, exit_price):
    if side == 'long':
        pnl_pct = (exit_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - exit_price) / entry_price
    notional = (BASE_AMOUNT / 100000) * entry_price
    pnl_usd = pnl_pct * notional
    return pnl_usd, pnl_pct


# ── ACCOUNT WS HELPERS ──────────────────────────────────────────────────
def ws_get_position():
    with acct_ws_lock: return dict(acct_ws_position)

def ws_is_flat():
    with acct_ws_lock: return acct_ws_position["size"] == 0.0

def ws_get_last_fill():
    with acct_ws_lock: return dict(acct_ws_last_trade) if acct_ws_last_trade else None

def ws_get_balance():
    with acct_ws_lock: return acct_ws_balance

async def ws_wait_for_flat(timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        if ws_is_flat(): return True
        await asyncio.sleep(0.2)
    return False

async def ws_wait_for_position(timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        if ws_get_position()["size"] > 0: return True
        await asyncio.sleep(0.2)
    return False


async def position_sync_check(signer, local_pos):
    """Detect and fix position mismatches every loop."""
    ws_pos = ws_get_position()
    if not local_pos.in_position and ws_pos["size"] > 0:
        is_long = ws_pos["sign"] > 0
        side_str = "LONG" if is_long else "SHORT"
        print(f"  SYNC: Naked {side_str} detected! Size={ws_pos['size']:.5f}")
        tg(f"SYNC: Naked {side_str}!\nSize: {ws_pos['size']:.5f} @ ${ws_pos['entry']:,.1f}\nAuto-closing...")
        try:
            await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
            await asyncio.sleep(1)
        except: pass
        try:
            _, _, err = await signer.create_market_order_limited_slippage(
                market_index=MARKET_INDEX, client_order_index=0,
                base_amount=int(ws_pos["size"] * 100000),
                max_slippage=0.01, is_ask=1 if is_long else 0, reduce_only=True)
            if not err:
                await asyncio.sleep(3)
                if ws_is_flat():
                    print(f"  SYNC: Closed"); tg(f"Naked position closed")
                else:
                    print(f"  SYNC: May still be open"); tg(f"Naked close may have failed!")
            else:
                print(f"  SYNC: Failed: {err}"); tg(f"Naked close failed: {err}")
        except Exception as e:
            print(f"  SYNC: Error: {e}"); tg(f"Naked close error: {e}")
        return True

    if local_pos.in_position and ws_pos["size"] == 0:
        rest_pos = api_get_position()
        if rest_pos and rest_pos["size"] == 0:
            print(f"  SYNC: Bot thinks {local_pos.side} but exchange flat")
            tg(f"SYNC: Position gone\nWas: {local_pos.side} @ ${local_pos.entry_price:,.0f}")
            local_pos.close("sync_flat")
            return True
    return False


# ── STATE & TG ──────────────────────────────────────────────────────────
def save_state(data):
    try:
        with open(STATE_FILE, "w") as f: json.dump(data, f)
    except: pass

def load_state():
    d = {"last_trade_time": 0, "last_direction": None,
         "daily_pnl": 0, "wins": 0, "losses": 0, "daily_trades": 0,
         "last_day": time.strftime("%d"), "consecutive_losses": 0, "last_loss_time": 0}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            for k, v in d.items():
                if k not in data: data[k] = v
            return data
    except: return d

def tg(msg):
    prefix = "PAPER " if PAPER_TRADE else ""
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": prefix + msg, "parse_mode": "HTML"}, timeout=5)
    except: pass


# ══════════════════════════════════════════════════════════════════════════
# SL ORDER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════
async def attach_sl_order(signer, entry, is_long):
    """Attach SL order on exchange — always present, non-negotiable."""
    try:
        if is_long:
            sl_p = entry * (1 - SL_PCT); ea = 1
            sl_l = int(sl_p * 0.999 * 10)
        else:
            sl_p = entry * (1 + SL_PCT); ea = 0
            sl_l = int(sl_p * 1.001 * 10)
        sl_t = int(sl_p * 10)
        _, _, err = await signer.create_sl_order(
            market_index=MARKET_INDEX, client_order_index=0,
            base_amount=BASE_AMOUNT, price=sl_l, is_ask=ea, trigger_price=sl_t)
        return err, sl_p
    except Exception as e:
        try:
            sl_o = CreateOrderTxReq(MarketIndex=MARKET_INDEX, ClientOrderIndex=0,
                BaseAmount=BASE_AMOUNT, Price=sl_l, IsAsk=ea,
                Type=signer.ORDER_TYPE_STOP_LOSS_LIMIT,
                TimeInForce=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                ReduceOnly=1, TriggerPrice=sl_t, OrderExpiry=-1)
            _, _, err = await signer.create_grouped_orders(
                grouping_type=signer.GROUPING_TYPE_ONE_CANCELS_THE_OTHER, orders=[sl_o])
            return err, sl_p
        except Exception as e2:
            return str(e2), 0

async def attach_sl_background(signer, is_long, entry_price):
    """Attach SL with staleness check."""
    expected_entry_time = local_pos.entry_time
    await asyncio.sleep(2)
    if local_pos.entry_time != expected_entry_time:
        print(f"  SL skipped -- position changed"); return
    for attempt in range(3):
        if local_pos.entry_time != expected_entry_time:
            print(f"  SL skipped -- position changed"); return
        try:
            err, sl_p = await attach_sl_order(signer, entry_price, is_long)
            if not err:
                local_pos.oco_attached = True; local_pos._save()
                tg(f"SL placed @ ${sl_p:,.0f}")
                return
            print(f"  SL attempt {attempt+1}: {err}")
        except Exception as e:
            print(f"  SL attempt {attempt+1}: {e}")
        await asyncio.sleep(2)
    if local_pos.entry_time == expected_entry_time:
        local_pos.oco_attached = False; local_pos._save()
        tg("SL order FAILED -- bot monitoring locally!")


# ══════════════════════════════════════════════════════════════════════════
# EXIT FUNCTIONS (unchanged safety infrastructure)
# ══════════════════════════════════════════════════════════════════════════
async def send_market_close(signer, is_long):
    try:
        await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
        await asyncio.sleep(1)
    except Exception as e:
        print(f"  Cancel error: {e}")
    try:
        _, _, err = await signer.create_market_order_limited_slippage(
            market_index=MARKET_INDEX, client_order_index=0,
            base_amount=BASE_AMOUNT, max_slippage=0.01,
            is_ask=1 if is_long else 0, reduce_only=True)
    except Exception as e:
        print(f"  ReduceOnly failed ({e})")
        if ws_is_flat():
            print(f"  WS shows flat already"); return True
        tg(f"ReduceOnly close failed! Close manually!\n{e}")
        err = str(e)
    if err:
        print(f"  Exit order failed: {err}"); tg(f"Exit failed: {err}"); return False
    return True

async def cancel_stale_orders(signer):
    try:
        await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
        print(f"  Cancelled stale orders")
    except Exception as e:
        print(f"  Stale order cancel failed: {e}")

async def close_position(signer, local_pos, reason, price):
    is_long = (local_pos.side == 'long')
    if PAPER_TRADE: return True
    print(f"  Closing: {reason}")
    success = await send_market_close(signer, is_long)
    if success:
        flat = await ws_wait_for_flat(timeout=8)
        if flat:
            print(f"  WS confirmed flat")
            await cancel_stale_orders(signer)
            local_pos.close(reason); return True
        else:
            print(f"  WS timeout -- checking REST...")
            pos = api_get_position()
            if pos and pos["size"] == 0:
                print(f"  REST confirms flat")
                await cancel_stale_orders(signer)
                local_pos.close(reason); return True
            elif pos and pos["size"] > 0:
                await asyncio.sleep(3)
                if ws_is_flat():
                    local_pos.close(reason); return True
                pos2 = api_get_position()
                if pos2 and pos2["size"] == 0:
                    local_pos.close(reason); return True
                success2 = await send_market_close(signer, is_long)
                if success2:
                    flat2 = await ws_wait_for_flat(timeout=8)
                    if not flat2: tg(f"Exit uncertain -- check position!")
                local_pos.close(reason); return True
            else:
                local_pos.close(reason); return True
    else:
        await asyncio.sleep(2)
        success2 = await send_market_close(signer, is_long)
        if success2:
            flat = await ws_wait_for_flat(timeout=8)
            if not flat:
                pos = api_get_position()
                if pos and pos["size"] > 0:
                    tg(f"Exit may have failed -- check position!")
            local_pos.close(reason); return True
        else:
            tg(f"EXIT FAILED TWICE! Close manually!\n{local_pos.side} @ ${local_pos.entry_price:,.0f}")
            return False


# ══════════════════════════════════════════════════════════════════════════
# STARTUP CLEANUP
# ══════════════════════════════════════════════════════════════════════════
async def startup_close_orphan(signer):
    print("  Checking for orphaned positions...")

    # Step 1: Read saved position state from disk — this is the ONLY reliable source for side
    saved_side = None
    saved_in_position = False
    try:
        with open(LOCAL_POS_FILE) as f:
            saved = json.load(f)
            saved_side = saved.get("side")          # 'long' or 'short'
            saved_in_position = saved.get("in_position", False)
            if saved_side:
                print(f"  Saved state: side={saved_side}, in_position={saved_in_position}")
    except Exception as e:
        print(f"  No saved state: {e}")

    for attempt in range(3):
        pos = api_get_position()
        if pos is None:
            print(f"  API failed ({attempt+1}/3)"); await asyncio.sleep(2); continue
        if pos["size"] == 0 and pos["open_orders"] == 0:
            print(f"  Clean. Balance: ${pos['balance']:.2f}")
            # Clear saved state if it thinks we're in a position
            if saved_in_position:
                try:
                    with open(LOCAL_POS_FILE, "w") as f:
                        json.dump({"in_position": False, "side": None, "entry_price": 0,
                                   "entry_time": 0, "size": 0, "sl_price": 0, "tp_price": 0,
                                   "oco_attached": False}, f)
                    print(f"  Cleared stale saved state")
                except: pass
            return pos["balance"]

        if pos["open_orders"] > 0:
            print(f"  Cancelling {pos['open_orders']} orphaned orders...")
            try:
                await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
                await asyncio.sleep(2)
            except Exception as e: print(f"  Cancel error: {e}")

        if pos["size"] > 0:
            # CRITICAL: Use saved state for direction, NOT the API sign (which is unreliable)
            if saved_side:
                is_long = (saved_side == 'long')
                side_str = saved_side.upper()
                print(f"  Orphaned {side_str} (from saved state): {pos['size']:.5f} BTC @ ${pos['entry']:,.1f}")
            else:
                # No saved state — we can't safely determine direction
                print(f"  Orphan detected: {pos['size']:.5f} BTC @ ${pos['entry']:,.1f}")
                print(f"  NO SAVED STATE — cannot determine side. Will NOT auto-close.")
                tg(f"ORPHAN DETECTED on startup\n{pos['size']:.5f} BTC @ ${pos['entry']:,.1f}\n\nNo saved state — cannot determine LONG or SHORT.\nAPI sign is unreliable.\nClose manually!")
                return None

            print(f"  Close action: {'SELL' if is_long else 'BUY'} {pos['size']:.5f} BTC")
            tg(f"Orphaned {side_str} on startup\n{pos['size']:.5f} @ ${pos['entry']:,.1f}\nAuto-closing with {'SELL' if is_long else 'BUY'}...")
            try:
                _, _, err = await signer.create_market_order_limited_slippage(
                    market_index=MARKET_INDEX, client_order_index=0,
                    base_amount=int(pos["size"] * 100000),
                    max_slippage=0.01, is_ask=1 if is_long else 0)
            except Exception as e:
                tg(f"Orphan close failed!\n{e}"); err = str(e)
            if err:
                print(f"  Close failed: {err}"); await asyncio.sleep(3); continue
            await asyncio.sleep(EXIT_SETTLE_SECS + 2)

            # Verify position is closed
            pos2 = api_get_position()
            if pos2 is None:
                print(f"  API failed on verify ({attempt+1}/3)"); await asyncio.sleep(2); continue
            if pos2["size"] == 0:
                print(f"  Orphan closed. Balance: ${pos2['balance']:.2f}")
                tg(f"Orphan closed\nBalance: ${pos2['balance']:.2f}")
                # Clear saved state
                try:
                    with open(LOCAL_POS_FILE, "w") as f:
                        json.dump({"in_position": False, "side": None, "entry_price": 0,
                                   "entry_time": 0, "size": 0, "sl_price": 0, "tp_price": 0,
                                   "oco_attached": False}, f)
                except: pass
                return pos2["balance"]

            # Position still open — check if it grew (would mean wrong direction)
            if pos2["size"] > pos["size"] * 1.5:
                print(f"  DANGER: Position GREW from {pos['size']:.5f} to {pos2['size']:.5f}!")
                print(f"  Wrong direction detected. ABORTING — close manually.")
                tg(f"ORPHAN CLOSE WRONG DIRECTION!\nPosition grew from {pos['size']:.5f} to {pos2['size']:.5f}\nABORTING — close manually!")
                return None

            print(f"  Still open: {pos2['size']:.5f} BTC ({attempt+1}/3)")
            await asyncio.sleep(2); continue

    tg(f"STARTUP FAILED: Could not close orphan after 3 attempts.\nClose manually.")
    return None


# ── GLOBALS ─────────────────────────────────────────────────────────────
state = load_state()
daily_pnl = 0.0; daily_trades = 0
trades_this_hour = 0; hour_reset_time = time.time()
last_trade_time = state["last_trade_time"]
last_entry_candle_min = 0  # track which 1m candle generated last entry — no re-entry on same candle
wins = 0; losses = 0
long_pnl = 0.0; short_pnl = 0.0; long_trades = 0; short_trades = 0
last_update_id = 0; paused = False; last_day = state["last_day"]
consecutive_losses = state.get("consecutive_losses", 0)
last_loss_time = state.get("last_loss_time", 0)
last_direction = state["last_direction"]

def track_direction_pnl(side, pnl):
    global long_pnl, short_pnl, long_trades, short_trades
    if side == 'long': long_pnl += pnl; long_trades += 1
    else: short_pnl += pnl; short_trades += 1

if last_day != time.strftime("%d"):
    last_day = time.strftime("%d")

local_pos = LocalPosition()
paper = PaperTrader(PAPER_STARTING_BALANCE) if PAPER_TRADE else None


# ══════════════════════════════════════════════════════════════════════════
# RSI ENTRY EVALUATION (LIVE RSI)
# ══════════════════════════════════════════════════════════════════════════
def get_live_rsi_ago(seconds):
    """Get live RSI from N seconds ago. Returns None if not available."""
    cutoff = time.time() - seconds
    for t, r in reversed(live_rsi_history):
        if t <= cutoff:
            return r
    return None

def evaluate_rsi_entry(price):
    """
    Live RSI entry signal — reacts in real-time, not waiting for candle close.
    Compares current live RSI to live RSI from 15s ago for direction.
    Returns (direction, rsi_live, rsi_5m, whale_confirmed) or (None, ..., False).
    """
    rsi_live = get_rsi_1m_live(price)
    rsi_5m = get_rsi_5m()
    rsi_ago = get_live_rsi_ago(RSI_DELTA_LOOKBACK)

    if rsi_live is None:
        return None, rsi_live, rsi_5m, False

    if rsi_ago is None:
        return None, rsi_live, rsi_5m, False

    rsi_delta = rsi_live - rsi_ago
    rsi_rising = rsi_delta >= RSI_MIN_DELTA
    rsi_falling = rsi_delta <= -RSI_MIN_DELTA

    # Price move confirmation — need real price displacement, not just RSI noise
    with tick_lock:
        ticks = list(tick_prices)
    price_move_ok = False
    if len(ticks) >= 10:
        cutoff_60s = time.time() - 60
        recent = [p for t, p in ticks if t > cutoff_60s]
        if len(recent) >= 5:
            price_range = (max(recent) - min(recent)) / recent[0]
            price_move_ok = price_range >= MIN_PRICE_MOVE_PCT / 100
    if not price_move_ok:
        return None, rsi_live, rsi_5m, False

    # 5m direction filter — no overrides, respect the trend
    long_ok = rsi_5m is None or rsi_5m >= RSI_5M_LONG_MIN
    short_ok = rsi_5m is None or rsi_5m <= RSI_5M_SHORT_MAX

    direction = None

    # LONG: live RSI in buy zone and rising over last 15s
    if long_ok and RSI_LONG_ZONE_LOW <= rsi_live <= RSI_LONG_ZONE_HIGH and rsi_rising:
        direction = 'long'

    # SHORT: live RSI in sell zone and falling over last 15s
    if short_ok and RSI_SHORT_ZONE_LOW <= rsi_live <= RSI_SHORT_ZONE_HIGH and rsi_falling:
        direction = 'short'

    if direction is None:
        return None, rsi_live, rsi_5m, False

    # Whale check
    if whale_against_entry(direction):
        print(f"  WHALE BLOCK: whale against {direction} in last {WHALE_BLOCK_WINDOW}s")
        return None, rsi_live, rsi_5m, False

    whale_conf = whale_confirms_entry(direction)

    print(f"  LIVE RSI SIGNAL: {direction.upper()} | now={rsi_live:.1f} (15s ago={rsi_ago:.1f} d={rsi_delta:+.1f})"
          + (f" | 5m={rsi_5m:.1f}" if rsi_5m else " | 5m=n/a")
          + (f" | WHALE" if whale_conf else ""))

    return direction, rsi_live, rsi_5m, whale_conf


# ══════════════════════════════════════════════════════════════════════════
# RSI EXIT CHECK
# ══════════════════════════════════════════════════════════════════════════
def check_rsi_exit(local_pos, price):
    """
    Check if RSI signals exit. Returns (should_exit, reason).
    Uses live RSI (includes current candle) for responsiveness.
    """
    if not local_pos.in_position:
        return False, ""

    rsi_live = get_rsi_1m_live(price)
    rsi_prev = get_rsi_1m()  # last closed candle
    if rsi_live is None or rsi_prev is None:
        return False, ""

    hold = local_pos.hold_time()
    profit_pct = local_pos.unrealized_pct(price)
    rsi_falling = rsi_live < rsi_prev - 0.5
    rsi_rising = rsi_live > rsi_prev + 0.5

    if local_pos.side == 'long':
        # RSI profit exit: RSI was in profit zone and turning down
        if rsi_live >= RSI_LONG_PROFIT_EXIT and rsi_falling and profit_pct > 0.0002:
            return True, "rsi_profit"
        # RSI stall: been holding 2min+, RSI falling, in profit
        if hold >= RSI_STALL_MIN_HOLD and rsi_falling and profit_pct > RSI_STALL_MIN_PROFIT:
            return True, "rsi_stall"
        # RSI fail: thesis completely wrong
        if rsi_live < RSI_LONG_FAIL:
            return True, "rsi_fail"
    else:  # short
        # RSI profit exit: RSI was in profit zone and turning up
        if rsi_live <= RSI_SHORT_PROFIT_EXIT and rsi_rising and profit_pct > 0.0002:
            return True, "rsi_profit"
        # RSI stall
        if hold >= RSI_STALL_MIN_HOLD and rsi_rising and profit_pct > RSI_STALL_MIN_PROFIT:
            return True, "rsi_stall"
        # RSI fail
        if rsi_live > RSI_SHORT_FAIL:
            return True, "rsi_fail"

    return False, ""


# ══════════════════════════════════════════════════════════════════════════
# TELEGRAM CHECK
# ══════════════════════════════════════════════════════════════════════════
def tg_check():
    global paused, last_update_id
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout": 0, "offset": last_update_id + 1}, timeout=5)
        for u in r.json().get("result", []):
            last_update_id = u.get("update_id", last_update_id)
            msg = u.get("message", {}).get("text", "").strip().lower()
            if msg == "/pause": paused = True; tg("Paused")
            elif msg == "/resume": paused = False; tg("Resumed")
            elif msg == "/status":
                price, _ = get_momentum()
                rsi_1m = get_rsi_1m_live(price) if price else None
                rsi_5m = get_rsi_5m()
                ie = get_imb_ema()

                pos_str = f"{local_pos.side} @ ${local_pos.entry_price:,.0f}" if local_pos.in_position else "flat"
                sl_str = " SL:ON" if local_pos.oco_attached else " SL:LOCAL" if local_pos.in_position else ""
                mode = "PAPER" if PAPER_TRADE else "LIVE"
                wr = f"{wins/(wins+losses)*100:.0f}%" if wins+losses > 0 else "N/A"

                rsi1_str = f"{rsi_1m:.1f}" if rsi_1m is not None else "n/a"
                rsi5_str = f"{rsi_5m:.1f}" if rsi_5m is not None else "n/a"
                candle_str = f"1m:{len(candles_1m)} 5m:{len(candles_5m)}"

                pnl_str = ""
                if local_pos.in_position and price:
                    pnl_str = f"\nUnreal: {local_pos.unrealized_pct(price)*100:+.3f}% hold:{local_pos.hold_time()}s"

                tg(f"v8.0 RSI [{mode}]\n${price:,.0f} | Imb:{ie:+.2f}\n"
                   f"RSI 1m:{rsi1_str} 5m:{rsi5_str}\n"
                   f"Candles: {candle_str}\n"
                   f"Pos: {pos_str}{sl_str}{pnl_str}\n"
                   f"Day: ${daily_pnl:.2f} | {wins}W/{losses}L ({wr})\n"
                   f"L: {long_trades}t ${long_pnl:+.2f} | S: {short_trades}t ${short_pnl:+.2f}\n"
                   f"Streak: {consecutive_losses}L | SL:{SL_PCT*100:.2f}% HS:{HARD_STOP_PCT*100:.2f}%")
            elif msg == "/trades" and PAPER_TRADE and paper:
                if not paper.trade_log: tg("No trades yet")
                else:
                    last5 = paper.trade_log[-5:]
                    lines = [f"{t['time']} {t['side']} {t['reason']} ${t['pnl_usd']:+.2f}" for t in last5]
                    tg("Last 5:\n" + "\n".join(lines) + f"\n\n{paper.summary()}")
            elif msg == "/help": tg("/pause /resume /status /trades /help")
    except: pass


# ══════════════════════════════════════════════════════════════════════════
# HELPER: handle exit PnL tracking
# ══════════════════════════════════════════════════════════════════════════
def handle_exit_pnl(side, entry, exit_p, reason, is_paper=False):
    """Common PnL tracking after any exit. Returns (pnl_usd, pnl_pct)."""
    global daily_pnl, daily_trades, wins, losses, consecutive_losses, last_loss_time
    global last_trade_time

    if is_paper and paper:
        pnl_usd, pnl_pct, fill = paper.execute_exit(side, entry, exit_p, reason)
        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses
        daily_trades = paper.total_trades
        if pnl_usd >= 0: consecutive_losses = 0
        else: consecutive_losses += 1; last_loss_time = time.time()
        return pnl_usd, pnl_pct
    else:
        pnl_usd, pnl_pct = calc_pnl_from_prices(side, entry, exit_p)
        daily_pnl += pnl_usd; daily_trades += 1
        track_direction_pnl(side, pnl_usd)
        # Breakeven exits don't count as wins or losses
        if reason == "breakeven":
            pass  # no change to win/loss/streak counters
        elif pnl_usd >= 0: wins += 1; consecutive_losses = 0
        else: losses += 1; consecutive_losses += 1; last_loss_time = time.time()
        return pnl_usd, pnl_pct


def do_save_state():
    save_state({"last_trade_time": last_trade_time, "last_direction": last_direction,
        "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
        "daily_trades": daily_trades, "last_day": last_day,
        "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})


# ══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════
async def main():
    global daily_pnl, daily_trades, trades_this_hour, hour_reset_time, last_trade_time
    global wins, losses, last_day, paused, last_direction
    global consecutive_losses, last_loss_time, bot_start_time, last_entry_candle_min

    mode_str = "PAPER" if PAPER_TRADE else "LIVE"
    print(f"RSI Bot v8.2 MOMENTUM [{mode_str}]")
    print(f"  RSI period: {RSI_PERIOD} | LIVE RSI | Size: {BASE_AMOUNT/100000:.4f} BTC")
    print(f"  Long zone: {RSI_LONG_ZONE_LOW}-{RSI_LONG_ZONE_HIGH} | Short zone: {RSI_SHORT_ZONE_LOW}-{RSI_SHORT_ZONE_HIGH}")
    print(f"  Delta: {RSI_MIN_DELTA}+ over {RSI_DELTA_LOOKBACK}s lookback")
    print(f"  5m filter: long>{RSI_5M_LONG_MIN} short<{RSI_5M_SHORT_MAX}")
    print(f"  Exit: profit@{RSI_LONG_PROFIT_EXIT}/{RSI_SHORT_PROFIT_EXIT} fail@{RSI_LONG_FAIL}/{RSI_SHORT_FAIL}")
    print(f"  SL:{SL_PCT*100:.2f}% HS:{HARD_STOP_PCT*100:.2f}% Trail:@{TRAIL_ACTIVATE_PCT*100:.2f}%/{TRAIL_DISTANCE_PCT*100:.3f}%")
    print(f"  Trail tiers: {TRAIL_DISTANCE_PCT*100:.2f}%→{TRAIL_MID_DISTANCE*100:.2f}%→{TRAIL_TIGHT_DISTANCE*100:.2f}%")
    print(f"  Stall cut: {EARLY_STALL_SECS}s | Hold:{MAX_HOLD_SECS}s | CD:{MIN_TRADE_INTERVAL}s")

    bot_start_time = time.time()
    threading.Thread(target=ws_thread, daemon=True).start()
    await asyncio.sleep(3)

    if not PAPER_TRADE:
        print("  Waiting for account WS snapshot...")
        acct_ws_ready.wait(timeout=10)
        if acct_ws_ready.is_set():
            print(f"  Account WS ready. Balance: ${ws_get_balance():.2f}")
        else:
            print(f"  Account WS not ready -- REST fallback")

    signer = None
    if not PAPER_TRADE:
        signer = lighter.SignerClient(url=LIGHTER_URL, account_index=ACCOUNT_INDEX,
            api_private_keys={API_KEY_INDEX: API_PRIVATE_KEY})
        signer.create_client(api_key_index=API_KEY_INDEX)

    # Bootstrap candles from API (skip 16-min warmup)
    print("  Bootstrapping candles from Lighter API...")
    bootstrap_candles()

    if local_pos.in_position:
        print(f"  Stale local position -- clearing")
        local_pos.close("stale_startup")
    unlock_entry()

    starting_bal = None
    if not PAPER_TRADE:
        starting_bal = await startup_close_orphan(signer)
        if starting_bal is None:
            tg("Bot aborted -- could not close orphan."); return

    rsi_1m_boot = compute_rsi(list(candles_1m), RSI_PERIOD)
    rsi_5m_boot = compute_rsi(list(candles_5m), RSI_PERIOD)
    rsi_boot_str = (f"RSI 1m:{rsi_1m_boot:.1f}" if rsi_1m_boot else "RSI 1m:--") + \
                   (f" 5m:{rsi_5m_boot:.1f}" if rsi_5m_boot else " 5m:--")
    tg(f"v8.0 RSI [{mode_str}]\n"
       f"RSI {RSI_PERIOD} LIVE | Long:{RSI_LONG_ZONE_LOW}-{RSI_LONG_ZONE_HIGH} Short:{RSI_SHORT_ZONE_LOW}-{RSI_SHORT_ZONE_HIGH} d{RSI_MIN_DELTA}/{RSI_DELTA_LOOKBACK}s\n"
       f"SL:{SL_PCT*100:.2f}% HS:{HARD_STOP_PCT*100:.2f}%\n"
       f"Trail:@{TRAIL_ACTIVATE_PCT*100:.2f}% dist:{TRAIL_DISTANCE_PCT*100:.3f}%\n"
       f"Whale confirm:{WHALE_MIN_BTC}+ BTC\n"
       f"Bootstrap: {len(candles_1m)} 1m / {len(candles_5m)} 5m candles | {rsi_boot_str}"
       + (f"\nPaper: ${paper.balance:.2f}" if PAPER_TRADE else f"\nBalance: ${starting_bal:.2f}"))

    last_tg_check = 0

    while True:
        try:
            now = time.time()
            if now - hour_reset_time > 3600: trades_this_hour = 0; hour_reset_time = now
            today = time.strftime("%d")
            if today != last_day:
                daily_pnl = 0.0; daily_trades = 0; wins = 0; losses = 0
                last_day = today; consecutive_losses = 0
                if PAPER_TRADE and paper: paper.daily_pnl = 0.0
                tg("Daily reset")
                do_save_state()
            if now - last_tg_check > 3: tg_check(); last_tg_check = now

            # Get price
            price_mom = get_momentum()
            if price_mom[0] is None: await asyncio.sleep(1); continue
            price, momentum = price_mom

            # Update candles every tick
            update_candles(price)

            # Position sync
            if not PAPER_TRADE and signer:
                sync_handled = await position_sync_check(signer, local_pos)
                if sync_handled: await asyncio.sleep(3); continue

            locked = is_entry_locked()
            cd = max(0, int(last_trade_time + MIN_TRADE_INTERVAL - now))
            hold = local_pos.hold_time()
            ie = get_imb_ema()

            # Status line with RSI — track live RSI history for entry signals
            rsi_1m = get_rsi_1m_live(price)
            if rsi_1m is not None:
                live_rsi_history.append((now, rsi_1m))
            rsi_5m = get_rsi_5m()
            r1_str = f"r1={rsi_1m:.1f}" if rsi_1m is not None else "r1=--"
            r5_str = f"r5={rsi_5m:.1f}" if rsi_5m is not None else "r5=--"
            pos_str = f"{local_pos.side[0].upper()}" if local_pos.in_position else "-"
            sl_tag = "SL" if local_pos.oco_attached else "L" if local_pos.in_position else ""
            lk = "LOCK" if locked else ""

            print(f"[{time.strftime('%H:%M:%S')}] ${price:,.2f} {r1_str} {r5_str} imb={ie:+.2f} pos={pos_str}{sl_tag} pnl=${daily_pnl:.2f} cd={cd}s hold={hold}s c1m={len(candles_1m)} {lk}")

            # ══════════════════════════════════════════════════════════
            # IN POSITION — exit checks
            # ══════════════════════════════════════════════════════════
            if local_pos.in_position:
                is_long = (local_pos.side == 'long')
                unrealized_pct = local_pos.unrealized_pct(price)

                # ── 1. HARD STOP BACKSTOP (0.20%) — needs 3 consecutive ticks ──
                global _hard_stop_tick_count
                hard_stop_breach = False
                if is_long and price < local_pos.entry_price * (1 - HARD_STOP_PCT):
                    hard_stop_breach = True
                elif not is_long and price > local_pos.entry_price * (1 + HARD_STOP_PCT):
                    hard_stop_breach = True

                if hard_stop_breach:
                    _hard_stop_tick_count += 1
                else:
                    _hard_stop_tick_count = 0

                if _hard_stop_tick_count >= 3:
                    print(f"  HARD STOP! {unrealized_pct*100:+.3f}%")
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "hard_stop", True)
                        tg(f"HARD STOP ${price:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close("hard_stop"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "hard_stop", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "hard_stop")
                            tg(f"HARD STOP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            last_trade_time = now; unlock_entry()
                    do_save_state()
                    await asyncio.sleep(3); continue

                # ── 2. SL CHECK (0.15%) ──
                if local_pos.check_sl(price):
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, local_pos.sl_price, "sl", True)
                        tg(f"SL ${local_pos.sl_price:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close("sl"); last_trade_time = now; unlock_entry()
                    elif local_pos.oco_attached:
                        if ws_is_flat():
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if fill else local_pos.sl_price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "sl")
                            await cancel_stale_orders(signer)
                            local_pos.close("sl"); last_trade_time = now; unlock_entry()
                            tg(f"SL ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                        else:
                            success = await close_position(signer, local_pos, "fast_sl", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "fast_sl")
                                last_trade_time = now; unlock_entry()
                                tg(f"SL ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                    else:
                        success = await close_position(signer, local_pos, "local_sl", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "local_sl")
                            last_trade_time = now; unlock_entry()
                            tg(f"SL ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                    do_save_state()
                    await asyncio.sleep(3); continue

                # ── 3. TRAILING STOP ──
                # Check whale against — tighten trail if whale against us
                whale_hit, whale_size = whale_against_position(local_pos.side)
                trail_dist = WHALE_TIGHTEN_TRAIL_PCT if (whale_hit and local_pos.trailing_active) else None
                if whale_hit and local_pos.trailing_active:
                    print(f"  WHALE AGAINST {whale_size:.1f} BTC -- tightening trail to {WHALE_TIGHTEN_TRAIL_PCT*100:.3f}%")

                trail_result = local_pos.update_trailing_stop(price, trail_dist)
                if trail_result == 'trail_exit':
                    best_pct = abs(local_pos.best_price - local_pos.entry_price) / local_pos.entry_price * 100
                    print(f"  TRAIL EXIT! Best: ${local_pos.best_price:,.1f} (+{best_pct:.3f}%)")
                    _s = local_pos.side; _e = local_pos.entry_price; _b = local_pos.best_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "trail_exit", True)
                        tg(f"TRAIL EXIT ${price:,.0f}\nBest: ${_b:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close("trail_exit"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "trail_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "trail_exit")
                            last_trade_time = now; unlock_entry()
                            tg(f"TRAIL EXIT ${exit_p:,.0f}\nBest: ${_b:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                    do_save_state()
                    await asyncio.sleep(1); continue

                # ── 3a. EARLY STALL CUT — not moving after 30s, bail ──
                if hold >= EARLY_STALL_SECS and hold < LOSER_TIME_CUT_SECS and not local_pos.trailing_active and unrealized_pct < EARLY_STALL_MIN_PROFIT:
                    print(f"  EARLY STALL — {hold}s in, only {unrealized_pct*100:+.3f}%")
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "early_stall", True)
                        tg(f"EARLY STALL {hold}s ${price:,.0f}\n${pnl_usd:+.3f}\n{paper.summary()}")
                        local_pos.close("early_stall"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "early_stall", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "early_stall")
                            last_trade_time = now; unlock_entry()
                            tg(f"EARLY STALL {hold}s ${exit_p:,.0f}\n${pnl_usd:+.3f} | Day: ${daily_pnl:.2f}")
                    do_save_state()
                    await asyncio.sleep(3); continue

                # ── 3b. BREAK-EVEN STOP — once up 0.05%, move stop to entry ──
                if not local_pos.trailing_active and unrealized_pct >= BREAKEVEN_ACTIVATE_PCT:
                    if not getattr(local_pos, '_breakeven_set', False):
                        local_pos._breakeven_set = True
                        print(f"  BREAKEVEN STOP activated at +{unrealized_pct*100:.3f}%")
                if getattr(local_pos, '_breakeven_set', False) and not local_pos.trailing_active:
                    # If price comes back to entry, exit at breakeven
                    be_hit = False
                    if is_long and price <= local_pos.entry_price:
                        be_hit = True
                    elif not is_long and price >= local_pos.entry_price:
                        be_hit = True
                    if be_hit:
                        print(f"  BREAKEVEN EXIT — was up, now back to entry")
                        _s = local_pos.side; _e = local_pos.entry_price
                        if PAPER_TRADE:
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "breakeven", True)
                            tg(f"BREAKEVEN ${price:,.0f}\n${pnl_usd:+.3f}\n{paper.summary()}")
                            local_pos.close("breakeven"); last_trade_time = now; unlock_entry()
                        else:
                            success = await close_position(signer, local_pos, "breakeven", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "breakeven")
                                last_trade_time = now; unlock_entry()
                                tg(f"BREAKEVEN ${exit_p:,.0f}\n${pnl_usd:+.3f} | Day: ${daily_pnl:.2f}")
                        do_save_state()
                        await asyncio.sleep(3); continue

                # ── 3c. LOSER TIME CUT — if red after 120s, cut the loss ──
                if hold >= LOSER_TIME_CUT_SECS and unrealized_pct < 0:
                    print(f"  LOSER CUT — {hold}s in, still red {unrealized_pct*100:+.3f}%")
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "loser_cut", True)
                        tg(f"LOSER CUT {hold}s ${price:,.0f}\n${pnl_usd:+.3f}\n{paper.summary()}")
                        local_pos.close("loser_cut"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "loser_cut", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "loser_cut")
                            last_trade_time = now; unlock_entry()
                            tg(f"LOSER CUT {hold}s ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                    do_save_state()
                    await asyncio.sleep(3); continue

                # ── 4. RSI EXIT (primary profit-taking) ──
                rsi_exit, rsi_reason = check_rsi_exit(local_pos, price)
                if rsi_exit:
                    rsi_now = get_rsi_1m_live(price)
                    print(f"  RSI EXIT ({rsi_reason}) RSI={rsi_now:.1f} pnl={unrealized_pct*100:+.3f}%")
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, rsi_reason, True)
                        tg(f"RSI {rsi_reason.upper()} ${price:,.0f}\nRSI={rsi_now:.1f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close(rsi_reason); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, rsi_reason, price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, rsi_reason)
                            last_trade_time = now; unlock_entry()
                            tg(f"RSI {rsi_reason.upper()} ${exit_p:,.0f}\nRSI={rsi_now:.1f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                    do_save_state()
                    await asyncio.sleep(1); continue

                # ── 5. WHALE PROTECTION (tighten or exit if whale against + in profit) ──
                if whale_hit and not local_pos.trailing_active and unrealized_pct > 0.0004:
                    print(f"  WHALE EXIT: {whale_size:.1f} BTC against, taking profit +{unrealized_pct*100:.3f}%")
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "whale_exit", True)
                        tg(f"WHALE EXIT ${price:,.0f}\n{whale_size:.1f} BTC against\n${pnl_usd:+.3f}\n{paper.summary()}")
                        local_pos.close("whale_exit"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "whale_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "whale_exit")
                            last_trade_time = now; unlock_entry()
                            tg(f"WHALE EXIT ${exit_p:,.0f}\n{whale_size:.1f} BTC against\n${pnl_usd:+.3f} | Day: ${daily_pnl:.2f}")
                    do_save_state()
                    await asyncio.sleep(1); continue

                # ── 6. TIME EXIT ──
                effective_max_hold = TRAIL_MAX_HOLD_SECS if local_pos.trailing_active else MAX_HOLD_SECS
                if hold > effective_max_hold:
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "time_exit", True)
                        tg(f"TIME EXIT {hold}s ${pnl_usd:+.3f}\n{paper.summary()}")
                        local_pos.close("time_exit"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "time_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "time_exit")
                            last_trade_time = now; unlock_entry()
                            tg(f"TIME EXIT {hold}s ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                    do_save_state()
                    await asyncio.sleep(1); continue
                else:
                    # Holding — log status
                    sl_tag = "SL" if local_pos.oco_attached else "LOCAL"
                    trail_str = f" trail=${local_pos.trail_stop_price:,.1f}" if local_pos.trailing_active else ""
                    rsi_str = f" rsi={rsi_1m:.1f}" if rsi_1m is not None else ""
                    print(f"  Holding {local_pos.side} ${local_pos.entry_price:,.0f} pnl={unrealized_pct*100:.3f}% {hold}s {sl_tag}{trail_str}{rsi_str}")

            # ══════════════════════════════════════════════════════════
            # ENTRY LOGIC (RSI-based)
            # ══════════════════════════════════════════════════════════
            elif not local_pos.in_position and not locked:
                if not is_warmed_up():
                    print(f"  Warming up... {len(candles_1m)}/{RSI_PERIOD+2} 1m candles")
                elif paused:
                    print("  Paused")
                elif daily_pnl <= -DAILY_LOSS_LIMIT:
                    print("  Daily loss limit")
                elif trades_this_hour >= MAX_TRADES_HOUR:
                    print("  Max trades/hr")
                elif cd > 0:
                    print(f"  CD {cd}s")
                elif consecutive_losses >= 3 and now - last_loss_time < LOSS_STREAK_COOLDOWN:
                    remaining = int(LOSS_STREAK_COOLDOWN - (now - last_loss_time))
                    print(f"  Loss streak CD ({consecutive_losses}L) {remaining}s")
                elif consecutive_losses > 0 and now - last_loss_time < LOSS_COOLDOWN_SECS:
                    remaining = int(LOSS_COOLDOWN_SECS - (now - last_loss_time))
                    print(f"  Loss CD {remaining}s")
                else:
                    direction, r1, r5, whale_conf = evaluate_rsi_entry(price)
                    if direction:
                        is_long_entry = (direction == 'long')
                        tag = "LONG" if is_long_entry else "SHORT"
                        whale_str = " +WHALE" if whale_conf else ""
                        r5_str = f" 5m={r5:.1f}" if r5 is not None else ""

                        if PAPER_TRADE:
                            fill = paper.execute_entry(direction, price)
                            lock_entry(); local_pos.open(direction, fill)
                            _hard_stop_tick_count = 0
                            local_pos.rsi_at_entry = r1
                            last_trade_time = now; last_direction = direction; trades_this_hour += 1
                            last_entry_candle_min = _candle_1m.get("minute", 0)
                            tg(f"{tag} ${fill:,.0f}{whale_str}\nRSI 1m={r1:.1f}{r5_str}\nSL:${local_pos.sl_price:,.0f}")
                            do_save_state()
                            await asyncio.sleep(5); continue
                        else:
                            if not ws_is_flat():
                                print(f"  Entry blocked: WS shows position open")
                                lock_entry(); await asyncio.sleep(10); continue

                            lock_entry()
                            print(f"  {tag} ${price:,.0f} RSI 1m={r1:.1f}{r5_str} imb={ie:+.2f}{whale_str}")
                            await cancel_stale_orders(signer)
                            _, _, err = await signer.create_market_order_limited_slippage(
                                market_index=MARKET_INDEX, client_order_index=0,
                                base_amount=BASE_AMOUNT, max_slippage=0.005,
                                is_ask=0 if is_long_entry else 1)
                            if err:
                                tg(f"Order failed: {err}"); unlock_entry()
                            else:
                                local_pos.open(direction, price)
                                _hard_stop_tick_count = 0
                                local_pos.rsi_at_entry = r1
                                last_trade_time = now; last_direction = direction; trades_this_hour += 1
                                last_entry_candle_min = _candle_1m.get("minute", 0)
                                tg(f"{tag} ${price:,.0f}{whale_str}\nRSI 1m={r1:.1f}{r5_str}\nSL:${local_pos.sl_price:,.0f}")
                                do_save_state()
                                asyncio.create_task(attach_sl_background(signer, is_long_entry, price))
                                await asyncio.sleep(5); continue
                    else:
                        # No signal — show live RSI + delta
                        r1_d = f"1m={r1:.1f}" if r1 is not None else "1m=--"
                        r5_d = f"5m={r5:.1f}" if r5 is not None else "5m=--"
                        rsi_ago = get_live_rsi_ago(RSI_DELTA_LOOKBACK)
                        d_str = f" d={r1 - rsi_ago:+.1f}" if r1 is not None and rsi_ago is not None else ""
                        print(f"  Scanning LIVE {r1_d}{d_str} {r5_d} imb={ie:+.2f}")

        except Exception as e:
            print(f"Loop error: {e}")
            traceback.print_exc()
            await asyncio.sleep(2)
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"FATAL CRASH: {e}")
        traceback.print_exc()
