#!/usr/bin/env python3
"""
RSI Bot v9.2 — 5m Momentum Scalper (RSI(7) + VWAP)

Strategy: 5m RSI(7) momentum + VWAP directional filter + 15m trend filter.
  1. 5m RSI(7) from Lighter REST API (fast response for 5m scalping)
  2. Entry: RSI delta (direction) + price move confirmation (momentum)
  3. VWAP directional filter (only long above VWAP, only short below)
  4. 15m RSI trend filter (only blocks extremes)
  5. 0.40% SL on exchange (2% limit padding — guaranteed fill)
  6. 3-tier trailing stop for exits
  7. RSI-based exits (profit, stall, fail)
  8. Whale flow confirmation + protection

Safety: SL always on exchange, position sync, no naked positions, no double orders
"""
import asyncio, time, requests, lighter, threading, json, collections, os, traceback, urllib.request, calendar
from datetime import datetime, timezone
from lighter.signer_client import CreateOrderTxReq
import websocket as ws_lib

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

# ── 5m RSI MOMENTUM STRATEGY (from Lighter REST API) ─────────────────────
RSI_PERIOD = 7                 # faster response for 5m scalping
LIGHTER_CANDLE_URL = "https://mainnet.zklighter.elliot.ai/api/v1/candles"
CANDLE_FETCH_COUNT = 200       # 200 candles — plenty for RSI + VWAP

# Entry zones: where 5m RSI must be to enter
RSI_LONG_ZONE_LOW = 20         # allow longs from oversold
RSI_LONG_ZONE_HIGH = 45        # allow longs below midline
RSI_SHORT_ZONE_LOW = 55        # allow shorts above midline
RSI_SHORT_ZONE_HIGH = 80       # allow shorts from overbought

# RSI delta: confirms direction (RSI must be moving our way)
RSI_MIN_DELTA = 3.0            # min RSI change over lookback period
RSI_DELTA_LOOKBACK = 600       # 10 min (~2 candles) lookback for RSI direction

# Price move confirmation: filters out noise, only trades during real moves
MIN_PRICE_MOVE_PCT = 0.10      # min 0.10% price range in last 60s

# 15m RSI trend filter — trade WITH the trend, not against it
RSI_15M_SHORT_MAX = 60         # don't short when 15m RSI > 60 (uptrend)
RSI_15M_LONG_MIN = 40          # don't long when 15m RSI < 40 (downtrend)

# RSI-based exits
RSI_LONG_PROFIT_EXIT = 65      # take profit when RSI hits this and turns down
RSI_SHORT_PROFIT_EXIT = 35     # take profit when RSI hits this and turns up
RSI_LONG_FAIL = 25             # thesis failed — cut loss (less extreme for 5m)
RSI_SHORT_FAIL = 75            # thesis failed — cut loss (less extreme for 5m)
RSI_STALL_MIN_HOLD = 300       # detect stalls after 5 min (let trades develop)
RSI_STALL_MIN_PROFIT = 0.0020  # only stall-exit if profit >= 0.20% (meaningful)

# RSI poll interval — fetch fresh 5m candles from Lighter API
RSI_POLL_INTERVAL = 30         # seconds between Lighter API candle fetches

# ── EXITS ────────────────────────────────────────────────────────────────
SL_PCT = 0.0040                # 0.40% SL (wider for 5m moves)
HARD_STOP_PCT = 0.0050         # 0.50% hard stop backstop
TRAIL_ACTIVATE_PCT = 0.0040    # 0.40% to activate trail (let it run first)
TRAIL_DISTANCE_PCT = 0.0015    # 0.15% trail distance (keep more profit)
TRAIL_MID_ACTIVATE = 0.0070    # 0.70%+ profit → mid tier
TRAIL_MID_DISTANCE = 0.0020    # 0.20% mid trail distance
TRAIL_TIGHT_ACTIVATE = 0.0100  # 1.00%+ profit → lock it in
TRAIL_TIGHT_DISTANCE = 0.0010  # 0.10% tight trail
MAX_HOLD_SECS = 900            # 15 min normal
TRAIL_MAX_HOLD_SECS = 1800     # 30 min trailing
LOCK_DURATION = 450

# ── WHALE FLOW ───────────────────────────────────────────────────────────
WHALE_MIN_BTC = 2.0
WHALE_CONFIRM_WINDOW = 15
WHALE_BLOCK_WINDOW = 10
WHALE_TIGHTEN_TRAIL_PCT = 0.0003

# ── COOLDOWNS ────────────────────────────────────────────────────────────
MIN_TRADE_INTERVAL = 60        # 60s between trades (5m candles = slower pace)
LOSS_COOLDOWN_SECS = 60        # pause after loss
LOSS_STREAK_COOLDOWN = 300     # 5 min after 3 losses
MAX_TRADES_HOUR = 10           # hard cap (5m = fewer trades)
DAILY_LOSS_LIMIT = 2.0

# ── KEPT FOR OB LOGGING ─────────────────────────────────────────────────
IMB_LEVELS = 5; IMB_EMA_ALPHA = 0.1
SMOOTH_TICKS = 10; LOOKBACK_TICKS = 30

EXIT_SETTLE_SECS = 5

STATE_FILE="/root/rsi_bot/.bot_state_v9.json"
LOCK_FILE="/root/rsi_bot/.entry_lock_v9"
LOCAL_POS_FILE="/root/rsi_bot/.local_position_v9.json"
TRADE_LOG_FILE="/root/rsi_bot/trade_log.jsonl"

# ── GLOBAL STATE ─────────────────────────────────────────────────────────
tick_prices=collections.deque(maxlen=1200); tick_lock=threading.Lock()
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
_hard_stop_tick_count = 0

# 5m/15m RSI state from Lighter API
rsi_5m_current = None          # current 5m RSI (for entry — includes forming candle)
rsi_5m_history = collections.deque(maxlen=60)   # (time, rsi) tuples for delta detection
rsi_15m_current = None         # current 15m RSI (trend filter)
vwap_current = None            # current VWAP (from today's 5m candles)
rsi_lock = threading.Lock()
last_rsi_fetch = 0             # timestamp of last API fetch

bot_start_time = time.time()
trade_entry_context = {}       # populated at entry, read at exit for trade log


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
        api_sign = int(pos.get("sign", 0))
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

    _FIELDS = ['in_position','side','entry_price','entry_time','size',
               'sl_price','tp_price','oco_attached',
               'trailing_active','best_price','trail_stop_price']

    def _load(self):
        try:
            with open(LOCAL_POS_FILE) as f:
                d = json.load(f)
                for k in self._FIELDS:
                    if k in d: setattr(self, k, d[k])
                if self.in_position:
                    trail_str = f" trail={'ON' if self.trailing_active else 'OFF'}" if hasattr(self, 'trailing_active') else ""
                    print(f"  Loaded: {self.side} @ ${self.entry_price:,.1f} oco={self.oco_attached}{trail_str}")
        except: pass

    def _save(self):
        try:
            with open(LOCAL_POS_FILE, "w") as f:
                json.dump({k: getattr(self, k, None) for k in self._FIELDS}, f)
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
        if side == 'long':
            self.sl_price = entry_price * (1 - SL_PCT)
            self.tp_price = entry_price * (1 + 0.005)  # effectively no TP, trail handles exit
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
        if not self.in_position: return False
        if self.side == 'long' and current_price <= self.sl_price: return True
        if self.side == 'short' and current_price >= self.sl_price: return True
        return False

    def update_trailing_stop(self, current_price, distance_override=None):
        if not self.in_position: return None
        profit_pct = self.unrealized_pct(current_price)

        if distance_override:
            trail_dist = distance_override
        elif profit_pct >= TRAIL_TIGHT_ACTIVATE:
            trail_dist = TRAIL_TIGHT_DISTANCE
        elif profit_pct >= TRAIL_MID_ACTIVATE:
            trail_dist = TRAIL_MID_DISTANCE
        else:
            trail_dist = TRAIL_DISTANCE_PCT

        if not self.trailing_active and profit_pct >= TRAIL_ACTIVATE_PCT:
            self.trailing_active = True
            self.best_price = current_price
            if self.side == 'long':
                self.trail_stop_price = current_price * (1 - trail_dist)
            else:
                self.trail_stop_price = current_price * (1 + trail_dist)
            self._save()
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
# WS FEED (orderbook for price + account for fills + trade tape for whales)
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

    def on_close(ws, *a): pass
    def on_open(ws):
        print("  WS connected")
        ws.send(json.dumps({"type": "subscribe", "channel": "order_book/1"}))
        ws.send(json.dumps({"type": "subscribe", "channel": f"account_all/{ACCOUNT_INDEX}"}))
        ws.send(json.dumps({"type": "subscribe", "channel": "trade/1"}))
        print(f"  Subscribed to account_all/{ACCOUNT_INDEX} + trade/1")
    while True:
        try:
            ws_lib.WebSocketApp("wss://mainnet.zklighter.elliot.ai/stream",
                on_message=on_msg, on_close=on_close, on_open=on_open
            ).run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"  WS error: {e}")
        time.sleep(2)


# ══════════════════════════════════════════════════════════════════════════
# LIGHTER REST API — RSI FROM CANDLES (exact match)
# ══════════════════════════════════════════════════════════════════════════
def compute_rsi(closes, period=14):
    """Compute RSI using Wilder's smoothing — matches Lighter/TradingView exactly."""
    if len(closes) < period + 1:
        return None
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


def fetch_candles_from_lighter(resolution, count=CANDLE_FETCH_COUNT):
    """Fetch candles from Lighter REST API. Returns list of close prices."""
    try:
        now = int(time.time())
        # Map resolution to seconds for start_timestamp
        res_seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        secs = res_seconds.get(resolution, 300)
        start = now - (count * secs)
        url = (f"{LIGHTER_CANDLE_URL}?market_id={MARKET_INDEX}&resolution={resolution}"
               f"&start_timestamp={start}&end_timestamp={now}&count_back={count}")
        resp = json.loads(urllib.request.urlopen(url, timeout=10).read().decode())
        if resp.get("code") != 200 or "c" not in resp:
            print(f"  Candle API error ({resolution}): {resp}")
            return []
        candles = resp["c"]
        # Include forming candle (last one) — matches Lighter's live RSI
        closes = [c["c"] for c in candles]
        return closes
    except Exception as e:
        print(f"  Candle fetch error ({resolution}): {e}")
        return []


def fetch_candles_ohlcv(resolution, count=CANDLE_FETCH_COUNT):
    """Fetch full OHLCV candles from Lighter REST API."""
    try:
        now = int(time.time())
        res_seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        secs = res_seconds.get(resolution, 300)
        start = now - (count * secs)
        url = (f"{LIGHTER_CANDLE_URL}?market_id={MARKET_INDEX}&resolution={resolution}"
               f"&start_timestamp={start}&end_timestamp={now}&count_back={count}")
        resp = json.loads(urllib.request.urlopen(url, timeout=10).read().decode())
        if resp.get("code") != 200 or "c" not in resp:
            print(f"  OHLCV API error ({resolution}): {resp}")
            return []
        return [{"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
                for c in resp["c"]]
    except Exception as e:
        print(f"  OHLCV fetch error ({resolution}): {e}")
        return []


def compute_vwap(candles):
    """Compute VWAP from today's candles. Resets at midnight UTC."""
    now_utc = datetime.now(timezone.utc)
    today_start = int(calendar.timegm(now_utc.replace(hour=0, minute=0, second=0, microsecond=0).timetuple()))
    today_candles = [c for c in candles if c["t"] >= today_start]

    cum_tpv = 0.0
    cum_vol = 0.0
    for c in today_candles:
        tp = (c["h"] + c["l"] + c["c"]) / 3
        cum_tpv += tp * c["v"]
        cum_vol += c["v"]

    if cum_vol == 0:
        return None
    return cum_tpv / cum_vol


def fetch_rsi_from_lighter():
    """Fetch 5m and 15m RSI from Lighter REST API. Called every 30s."""
    global rsi_5m_current, rsi_15m_current, vwap_current, last_rsi_fetch

    # Fetch 5m OHLCV (for both RSI and VWAP)
    ohlcv_5m = fetch_candles_ohlcv("5m", CANDLE_FETCH_COUNT)
    if ohlcv_5m and len(ohlcv_5m) >= RSI_PERIOD + 2:
        closes_5m = [c["c"] for c in ohlcv_5m]
        new_rsi_5m = compute_rsi(closes_5m, RSI_PERIOD)
        new_vwap = compute_vwap(ohlcv_5m)
        with rsi_lock:
            rsi_5m_current = new_rsi_5m
            vwap_current = new_vwap
            if new_rsi_5m is not None:
                rsi_5m_history.append((time.time(), new_rsi_5m))
        if new_rsi_5m is not None:
            ago = get_rsi_5m_ago(RSI_DELTA_LOOKBACK)
            delta_str = f"Δ{new_rsi_5m - ago:+.1f}" if ago else "Δ--"
            vwap_str = f" VWAP=${new_vwap:,.0f}" if new_vwap else ""
            print(f"  5m RSI: {new_rsi_5m:.1f} ({delta_str}){vwap_str} [{len(closes_5m)} candles]")

    # Fetch 15m candles (trend filter — RSI only, no VWAP needed)
    closes_15m = fetch_candles_from_lighter("15m", CANDLE_FETCH_COUNT)
    if closes_15m and len(closes_15m) >= RSI_PERIOD + 2:
        new_rsi_15m = compute_rsi(closes_15m, RSI_PERIOD)
        with rsi_lock:
            rsi_15m_current = new_rsi_15m
        if new_rsi_15m is not None:
            print(f"  15m RSI: {new_rsi_15m:.1f} [{len(closes_15m)} candles]")

    last_rsi_fetch = time.time()


# ══════════════════════════════════════════════════════════════════════════
# RSI HELPERS
# ══════════════════════════════════════════════════════════════════════════
def get_rsi_5m_ago(seconds):
    """Get 5m RSI from N seconds ago (for delta detection)."""
    cutoff = time.time() - seconds
    for t, r in reversed(rsi_5m_history):
        if t <= cutoff:
            return r
    return None


# ══════════════════════════════════════════════════════════════════════════
# ENTRY EVALUATION — 5m RSI momentum + price move confirmation
# ══════════════════════════════════════════════════════════════════════════
def evaluate_momentum_entry(price):
    """
    Momentum entry: 5m RSI delta + price move confirmation.
    Long:  RSI in 20-45, rising 3+ in 600s, price moved 0.10%+ in 60s
    Short: RSI in 55-80, falling 3+ in 600s, price moved 0.10%+ in 60s
    + 15m RSI trend filter (only blocks extremes)
    + VWAP directional filter (only long above VWAP, only short below)
    + Whale block/confirm
    Returns (direction, rsi_5m, rsi_15m, whale_confirmed) or (None, ...)
    """
    with rsi_lock:
        rsi_5m = rsi_5m_current
        rsi_15m = rsi_15m_current
        vwap = vwap_current

    if rsi_5m is None:
        return None, rsi_5m, rsi_15m, False

    rsi_ago = get_rsi_5m_ago(RSI_DELTA_LOOKBACK)
    if rsi_ago is None:
        return None, rsi_5m, rsi_15m, False

    rsi_delta = rsi_5m - rsi_ago
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
        return None, rsi_5m, rsi_15m, False

    # 15m RSI trend filter — only blocks extremes
    long_ok = rsi_15m is None or rsi_15m >= RSI_15M_LONG_MIN
    short_ok = rsi_15m is None or rsi_15m <= RSI_15M_SHORT_MAX

    # VWAP directional filter — only long above VWAP, only short below
    if vwap is not None:
        long_ok = long_ok and (price > vwap)
        short_ok = short_ok and (price < vwap)

    direction = None

    # LONG: RSI in buy zone and rising
    if long_ok and RSI_LONG_ZONE_LOW <= rsi_5m <= RSI_LONG_ZONE_HIGH and rsi_rising:
        direction = 'long'

    # SHORT: RSI in sell zone and falling
    if short_ok and RSI_SHORT_ZONE_LOW <= rsi_5m <= RSI_SHORT_ZONE_HIGH and rsi_falling:
        direction = 'short'

    if direction is None:
        return None, rsi_5m, rsi_15m, False

    # Whale check
    if whale_against_entry(direction):
        print(f"  WHALE BLOCK: whale against {direction} in last {WHALE_BLOCK_WINDOW}s")
        return None, rsi_5m, rsi_15m, False

    whale_conf = whale_confirms_entry(direction)

    print(f"  MOMENTUM SIGNAL: {direction.upper()} | 5m={rsi_5m:.1f} (ago={rsi_ago:.1f} Δ{rsi_delta:+.1f})"
          + (f" | 15m={rsi_15m:.1f}" if rsi_15m else " | 15m=n/a")
          + (f" | +WHALE" if whale_conf else ""))

    return direction, rsi_5m, rsi_15m, whale_conf


# ══════════════════════════════════════════════════════════════════════════
# RSI EXIT CHECK
# ══════════════════════════════════════════════════════════════════════════
def check_rsi_exit(local_pos, price):
    """
    RSI-based exit signals:
    - rsi_profit: RSI hit profit zone and reversed → take profit
    - rsi_stall: holding 5min+, RSI reversing, small profit → take it
    - rsi_fail: RSI went extreme wrong way → thesis failed, cut loss
    """
    if not local_pos.in_position:
        return False, ""

    with rsi_lock:
        rsi_5m = rsi_5m_current
    rsi_ago = get_rsi_5m_ago(300)

    if rsi_5m is None or rsi_ago is None:
        return False, ""

    hold = local_pos.hold_time()
    profit_pct = local_pos.unrealized_pct(price)
    rsi_falling = rsi_5m < rsi_ago - 0.5
    rsi_rising = rsi_5m > rsi_ago + 0.5

    if local_pos.side == 'long':
        if rsi_5m >= RSI_LONG_PROFIT_EXIT and rsi_falling and profit_pct > 0.0002:
            return True, "rsi_profit"
        if hold >= RSI_STALL_MIN_HOLD and rsi_falling and profit_pct > RSI_STALL_MIN_PROFIT:
            return True, "rsi_stall"
        if rsi_5m < RSI_LONG_FAIL:
            return True, "rsi_fail"
    else:
        if rsi_5m <= RSI_SHORT_PROFIT_EXIT and rsi_rising and profit_pct > 0.0002:
            return True, "rsi_profit"
        if hold >= RSI_STALL_MIN_HOLD and rsi_rising and profit_pct > RSI_STALL_MIN_PROFIT:
            return True, "rsi_stall"
        if rsi_5m > RSI_SHORT_FAIL:
            return True, "rsi_fail"

    return False, ""


# ══════════════════════════════════════════════════════════════════════════
# WHALE FLOW HELPERS
# ══════════════════════════════════════════════════════════════════════════
def get_recent_whales(seconds=15):
    with tape_lock:
        cutoff = time.time() - seconds
        return [t for t in tape_recent if t["time"] > cutoff and t["size"] >= WHALE_MIN_BTC]

def whale_against_entry(direction):
    whales = get_recent_whales(WHALE_BLOCK_WINDOW)
    if not whales: return False
    if direction == 'long':
        return any(w["side"] == "sell" for w in whales)
    else:
        return any(w["side"] == "buy" for w in whales)

def whale_confirms_entry(direction):
    whales = get_recent_whales(WHALE_CONFIRM_WINDOW)
    if not whales: return False
    if direction == 'long':
        return any(w["side"] == "buy" for w in whales)
    else:
        return any(w["side"] == "sell" for w in whales)

def whale_against_position(side):
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


# ── SIGNAL HELPERS (for logging) ─────────────────────────────────────────
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
    """
    Attach SL on exchange. Uses wide limit price (2%) to guarantee fill on gap.
    Always reduce_only to prevent opening reverse positions.
    """
    try:
        if is_long:
            # SL for LONG = SELL when price drops to trigger
            sl_p = entry * (1 - SL_PCT)      # trigger price
            ea = 1                             # is_ask=1 (sell)
            sl_l = int(sl_p * 0.98 * 10)      # limit 2% BELOW trigger (guarantees fill)
        else:
            # SL for SHORT = BUY when price rises to trigger
            sl_p = entry * (1 + SL_PCT)       # trigger price
            ea = 0                             # is_ask=0 (buy)
            sl_l = int(sl_p * 1.02 * 10)      # limit 2% ABOVE trigger (guarantees fill)
        sl_t = int(sl_p * 10)
        print(f"  SL order: trigger=${sl_p:,.1f} limit={sl_l} is_ask={ea} reduce_only=True")
        _, _, err = await signer.create_sl_order(
            market_index=MARKET_INDEX, client_order_index=0,
            base_amount=BASE_AMOUNT, price=sl_l, is_ask=ea,
            trigger_price=sl_t, reduce_only=True)
        return err, sl_p
    except Exception as e:
        print(f"  SL primary failed: {e}, trying fallback...")
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
# EXIT FUNCTIONS
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

    saved_side = None
    saved_in_position = False
    try:
        with open(LOCAL_POS_FILE) as f:
            saved = json.load(f)
            saved_side = saved.get("side")
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
            if saved_side:
                is_long = (saved_side == 'long')
                side_str = saved_side.upper()
                print(f"  Orphaned {side_str} (from saved state): {pos['size']:.5f} BTC @ ${pos['entry']:,.1f}")
            else:
                print(f"  Orphan detected: {pos['size']:.5f} BTC @ ${pos['entry']:,.1f}")
                print(f"  NO SAVED STATE — cannot determine side. Will NOT auto-close.")
                tg(f"ORPHAN DETECTED on startup\n{pos['size']:.5f} BTC @ ${pos['entry']:,.1f}\n\nNo saved state — cannot determine LONG or SHORT.\nClose manually!")
                return None

            print(f"  Close action: {'SELL' if is_long else 'BUY'} {pos['size']:.5f} BTC")
            tg(f"Orphaned {side_str} on startup\n{pos['size']:.5f} @ ${pos['entry']:,.1f}\nAuto-closing with {'SELL' if is_long else 'BUY'}...")
            try:
                _, _, err = await signer.create_market_order_limited_slippage(
                    market_index=MARKET_INDEX, client_order_index=0,
                    base_amount=int(pos["size"] * 100000),
                    max_slippage=0.01, is_ask=1 if is_long else 0, reduce_only=True)
            except Exception as e:
                tg(f"Orphan close failed!\n{e}"); err = str(e)
            if err:
                print(f"  Close failed: {err}"); await asyncio.sleep(3); continue
            await asyncio.sleep(EXIT_SETTLE_SECS + 2)

            pos2 = api_get_position()
            if pos2 is None:
                print(f"  API failed on verify ({attempt+1}/3)"); await asyncio.sleep(2); continue
            if pos2["size"] == 0:
                print(f"  Orphan closed. Balance: ${pos2['balance']:.2f}")
                tg(f"Orphan closed\nBalance: ${pos2['balance']:.2f}")
                try:
                    with open(LOCAL_POS_FILE, "w") as f:
                        json.dump({"in_position": False, "side": None, "entry_price": 0,
                                   "entry_time": 0, "size": 0, "sl_price": 0, "tp_price": 0,
                                   "oco_attached": False}, f)
                except: pass
                return pos2["balance"]

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
                ie = get_imb_ema()
                with rsi_lock:
                    r5m = rsi_5m_current; r15m = rsi_15m_current; vwap = vwap_current

                pos_str = f"{local_pos.side} @ ${local_pos.entry_price:,.0f}" if local_pos.in_position else "flat"
                sl_str = " SL:ON" if local_pos.oco_attached else " SL:LOCAL" if local_pos.in_position else ""
                mode = "PAPER" if PAPER_TRADE else "LIVE"
                wr = f"{wins/(wins+losses)*100:.0f}%" if wins+losses > 0 else "N/A"

                r5m_str = f"{r5m:.1f}" if r5m is not None else "n/a"
                r15m_str = f"{r15m:.1f}" if r15m is not None else "n/a"
                vwap_s = f"${vwap:,.0f}" if vwap is not None else "n/a"

                pnl_str = ""
                if local_pos.in_position and price:
                    pnl_str = f"\nUnreal: {local_pos.unrealized_pct(price)*100:+.3f}% hold:{local_pos.hold_time()}s"

                tg(f"v9.2 RSI({RSI_PERIOD})+VWAP [{mode}]\n${price:,.0f} | Imb:{ie:+.2f}\n"
                   f"RSI 5m:{r5m_str} 15m:{r15m_str}\n"
                   f"VWAP: {vwap_s}\n"
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
# TRADE LOG
# ══════════════════════════════════════════════════════════════════════════
def log_trade(side, entry, exit_p, reason, pnl_usd, pnl_pct, hold_secs):
    """Append a completed trade to the JSONL trade log."""
    global trade_entry_context
    ctx = trade_entry_context
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "side": side,
        "entry": round(entry, 1),
        "exit": round(exit_p, 1),
        "pnl_usd": round(pnl_usd, 4),
        "pnl_pct": round(pnl_pct * 100, 4),
        "reason": reason,
        "hold_secs": hold_secs,
        "rsi_5m": ctx.get("rsi_5m"),
        "rsi_15m": ctx.get("rsi_15m"),
        "vwap": ctx.get("vwap"),
        "whale": ctx.get("whale", False),
    }
    try:
        with open(TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"  Trade log error: {e}")
    trade_entry_context = {}


# ══════════════════════════════════════════════════════════════════════════
# PNL TRACKING
# ══════════════════════════════════════════════════════════════════════════
def handle_exit_pnl(side, entry, exit_p, reason, is_paper=False):
    global daily_pnl, daily_trades, wins, losses, consecutive_losses, last_loss_time
    global last_trade_time

    hold_secs = local_pos.hold_time()

    if is_paper and paper:
        pnl_usd, pnl_pct, fill = paper.execute_exit(side, entry, exit_p, reason)
        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses
        daily_trades = paper.total_trades
        if pnl_usd >= 0: consecutive_losses = 0
        else: consecutive_losses += 1; last_loss_time = time.time()
        log_trade(side, entry, exit_p, reason, pnl_usd, pnl_pct, hold_secs)
        return pnl_usd, pnl_pct
    else:
        pnl_usd, pnl_pct = calc_pnl_from_prices(side, entry, exit_p)
        daily_pnl += pnl_usd; daily_trades += 1
        track_direction_pnl(side, pnl_usd)
        if pnl_usd >= 0: wins += 1; consecutive_losses = 0
        else: losses += 1; consecutive_losses += 1; last_loss_time = time.time()
        log_trade(side, entry, exit_p, reason, pnl_usd, pnl_pct, hold_secs)
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
    global consecutive_losses, last_loss_time, bot_start_time
    global last_rsi_fetch
    global long_pnl, short_pnl, long_trades, short_trades

    mode_str = "PAPER" if PAPER_TRADE else "LIVE"
    print(f"RSI Bot v9.2 — 5m Momentum Scalper (RSI({RSI_PERIOD}) + VWAP) [{mode_str}]")
    print(f"  Strategy: 5m RSI({RSI_PERIOD}) entry + VWAP filter + 15m trend filter")
    print(f"  Entry: Long {RSI_LONG_ZONE_LOW}-{RSI_LONG_ZONE_HIGH} rising | Short {RSI_SHORT_ZONE_LOW}-{RSI_SHORT_ZONE_HIGH} falling")
    print(f"  Filters: RSI Δ>={RSI_MIN_DELTA} in {RSI_DELTA_LOOKBACK}s + price move >={MIN_PRICE_MOVE_PCT}%")
    print(f"  VWAP: long only above VWAP, short only below VWAP")
    print(f"  15m filter: long>={RSI_15M_LONG_MIN} short<={RSI_15M_SHORT_MAX}")
    print(f"  Size: {BASE_AMOUNT/100000:.4f} BTC | SL:{SL_PCT*100:.2f}% HS:{HARD_STOP_PCT*100:.2f}%")
    print(f"  Trail: @{TRAIL_ACTIVATE_PCT*100:.2f}% → {TRAIL_DISTANCE_PCT*100:.2f}%/{TRAIL_MID_DISTANCE*100:.2f}%/{TRAIL_TIGHT_DISTANCE*100:.2f}%")
    print(f"  RSI source: Lighter REST API ({CANDLE_FETCH_COUNT} candles, poll every {RSI_POLL_INTERVAL}s)")

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

    # Bootstrap RSI + VWAP from Lighter API
    print("  Fetching initial RSI + VWAP from Lighter API...")
    fetch_rsi_from_lighter()
    with rsi_lock:
        r5m_boot = rsi_5m_current; r15m_boot = rsi_15m_current; vwap_boot = vwap_current
    r5m_str = f"{r5m_boot:.1f}" if r5m_boot else "--"
    r15m_str = f"{r15m_boot:.1f}" if r15m_boot else "--"
    vwap_str = f"${vwap_boot:,.0f}" if vwap_boot else "--"
    print(f"  Bootstrap: 5m RSI={r5m_str} 15m RSI={r15m_str} VWAP={vwap_str}")

    unlock_entry()

    # IMPORTANT: orphan cleanup FIRST — it reads saved state to determine side.
    # Only clear local_pos AFTER orphan cleanup is done.
    starting_bal = None
    if not PAPER_TRADE:
        starting_bal = await startup_close_orphan(signer)
        if starting_bal is None:
            tg("Bot aborted -- could not close orphan."); return

    # Now safe to clear stale local state (orphan cleanup already read it)
    if local_pos.in_position:
        print(f"  Stale local position -- clearing")
        local_pos.close("stale_startup")

    tg(f"v9.2 RSI({RSI_PERIOD}) + VWAP [{mode_str}]\n"
       f"Entry: 5m RSI Δ{RSI_MIN_DELTA}+ in {RSI_DELTA_LOOKBACK}s\n"
       f"Long: {RSI_LONG_ZONE_LOW}-{RSI_LONG_ZONE_HIGH} | Short: {RSI_SHORT_ZONE_LOW}-{RSI_SHORT_ZONE_HIGH}\n"
       f"VWAP: long above / short below\n"
       f"15m filter: L>={RSI_15M_LONG_MIN} S<={RSI_15M_SHORT_MAX}\n"
       f"SL:{SL_PCT*100:.2f}% HS:{HARD_STOP_PCT*100:.2f}%\n"
       f"RSI: 5m={r5m_str} 15m={r15m_str} VWAP={vwap_str}\n"
       f"Source: Lighter REST API (poll {RSI_POLL_INTERVAL}s)"
       + (f"\nPaper: ${paper.balance:.2f}" if PAPER_TRADE else f"\nBalance: ${starting_bal:.2f}"))

    last_tg_check = 0
    last_signal_check = 0  # track when we last evaluated entry signal

    while True:
        try:
            now = time.time()
            if now - hour_reset_time > 3600: trades_this_hour = 0; hour_reset_time = now
            today = time.strftime("%d")
            if today != last_day:
                daily_pnl = 0.0; daily_trades = 0; wins = 0; losses = 0
                long_pnl = 0.0; short_pnl = 0.0; long_trades = 0; short_trades = 0
                last_day = today; consecutive_losses = 0
                if PAPER_TRADE and paper: paper.daily_pnl = 0.0
                tg("Daily reset")
                do_save_state()
            if now - last_tg_check > 3: tg_check(); last_tg_check = now

            # Get price from WS orderbook
            price_mom = get_momentum()
            if price_mom[0] is None: await asyncio.sleep(1); continue
            price, momentum = price_mom

            # Fetch fresh RSI from Lighter API every 30s
            rsi_refreshed = False
            if now - last_rsi_fetch >= RSI_POLL_INTERVAL:
                fetch_rsi_from_lighter()
                rsi_refreshed = True

            # Position sync
            if not PAPER_TRADE and signer:
                sync_handled = await position_sync_check(signer, local_pos)
                if sync_handled: await asyncio.sleep(3); continue

            locked = is_entry_locked()
            cd = max(0, int(last_trade_time + MIN_TRADE_INTERVAL - now))
            hold = local_pos.hold_time()
            ie = get_imb_ema()

            with rsi_lock:
                r5m = rsi_5m_current; r15m = rsi_15m_current; vwap = vwap_current
            r5m_str = f"r5m={r5m:.1f}" if r5m is not None else "r5m=--"
            r15m_str = f"r15m={r15m:.1f}" if r15m is not None else "r15m=--"
            vwap_str = f"vwap=${vwap:,.0f}" if vwap is not None else "vwap=--"
            pos_str = f"{local_pos.side[0].upper()}" if local_pos.in_position else "-"
            sl_tag = "SL" if local_pos.oco_attached else "L" if local_pos.in_position else ""
            lk = "LOCK" if locked else ""

            print(f"[{time.strftime('%H:%M:%S')}] ${price:,.2f} {r5m_str} {r15m_str} {vwap_str} imb={ie:+.2f} pos={pos_str}{sl_tag} pnl=${daily_pnl:.2f} cd={cd}s hold={hold}s {lk}")

            # ══════════════════════════════════════════════════════════
            # IN POSITION — exit checks
            # ══════════════════════════════════════════════════════════
            if local_pos.in_position:
                is_long = (local_pos.side == 'long')
                unrealized_pct = local_pos.unrealized_pct(price)

                # ── 1. HARD STOP BACKSTOP ──
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

                # ── 2. SL CHECK ──
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

                # ── 4. RSI EXIT ──
                if rsi_refreshed:
                    rsi_exit, rsi_reason = check_rsi_exit(local_pos, price)
                    if rsi_exit:
                        print(f"  RSI EXIT: {rsi_reason} | pnl={unrealized_pct*100:+.3f}%")
                        _s = local_pos.side; _e = local_pos.entry_price
                        if PAPER_TRADE:
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, rsi_reason, True)
                            tg(f"RSI EXIT ({rsi_reason}) ${price:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                            local_pos.close(rsi_reason); last_trade_time = now; unlock_entry()
                        else:
                            success = await close_position(signer, local_pos, rsi_reason, price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, rsi_reason)
                                last_trade_time = now; unlock_entry()
                                tg(f"RSI EXIT ({rsi_reason}) ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                        do_save_state()
                        await asyncio.sleep(1); continue

                # ── 5. WHALE PROTECTION ──
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
                    sl_tag = "SL" if local_pos.oco_attached else "LOCAL"
                    trail_str = f" trail=${local_pos.trail_stop_price:,.1f}" if local_pos.trailing_active else ""
                    print(f"  Holding {local_pos.side} ${local_pos.entry_price:,.0f} pnl={unrealized_pct*100:.3f}% {hold}s {sl_tag}{trail_str}")

            # ══════════════════════════════════════════════════════════
            # ENTRY LOGIC — 5m RSI reversal (only check when RSI refreshed)
            # ══════════════════════════════════════════════════════════
            elif not local_pos.in_position and not locked:
                if paused:
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
                elif rsi_refreshed:
                    # Evaluate momentum entry when we have fresh RSI data
                    direction, r5m_val, r15m_val, whale_conf = evaluate_momentum_entry(price)
                    if direction:
                        is_long_entry = (direction == 'long')
                        tag = "LONG" if is_long_entry else "SHORT"
                        whale_str = " +WHALE" if whale_conf else ""
                        r5m_disp = f" 5m={r5m_val:.1f}" if r5m_val is not None else ""
                        r15m_disp = f" 15m={r15m_val:.1f}" if r15m_val is not None else ""
                        with rsi_lock:
                            vwap_entry = vwap_current
                        vwap_disp = f" VWAP=${vwap_entry:,.0f}" if vwap_entry is not None else ""

                        trade_entry_context = {"rsi_5m": r5m_val, "rsi_15m": r15m_val,
                            "vwap": vwap_entry, "whale": whale_conf}

                        if PAPER_TRADE:
                            fill = paper.execute_entry(direction, price)
                            lock_entry(); local_pos.open(direction, fill)
                            _hard_stop_tick_count = 0
                            last_trade_time = now; last_direction = direction; trades_this_hour += 1
                            tg(f"{tag} ${fill:,.0f}{whale_str}\nRSI{r5m_disp}{r15m_disp}{vwap_disp}\nSL:${local_pos.sl_price:,.0f}")
                            do_save_state()
                            await asyncio.sleep(5); continue
                        else:
                            if not ws_is_flat():
                                print(f"  Entry blocked: WS shows position open")
                                lock_entry(); await asyncio.sleep(10); continue

                            lock_entry()
                            print(f"  {tag} ${price:,.0f} RSI{r5m_disp}{r15m_disp}{vwap_disp} imb={ie:+.2f}{whale_str}")
                            await cancel_stale_orders(signer)
                            _, _, err = await signer.create_market_order_limited_slippage(
                                market_index=MARKET_INDEX, client_order_index=0,
                                base_amount=BASE_AMOUNT, max_slippage=0.005,
                                is_ask=0 if is_long_entry else 1)
                            if err:
                                tg(f"Order failed: {err}"); unlock_entry()
                            else:
                                # Wait for actual fill price from WS
                                filled = await ws_wait_for_position(timeout=5)
                                if filled:
                                    fill = ws_get_last_fill()
                                    entry_price = fill["price"] if (fill and time.time() - fill["time"] < 10) else price
                                else:
                                    entry_price = price  # fallback to mid-price
                                local_pos.open(direction, entry_price)
                                _hard_stop_tick_count = 0
                                last_trade_time = now; last_direction = direction; trades_this_hour += 1
                                tg(f"{tag} ${entry_price:,.0f}{whale_str}\nRSI{r5m_disp}{r15m_disp}{vwap_disp}\nSL:${local_pos.sl_price:,.0f}")
                                do_save_state()
                                asyncio.create_task(attach_sl_background(signer, is_long_entry, entry_price))
                                await asyncio.sleep(5); continue

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
