#!/usr/bin/env python3
"""
RSI Bot v11 — Mean-Reversion Scalper (RSI(7) + VWAP σ + ADX)

Strategy: RSI zone (35/65) + RSI turning + VWAP σ bands + ADX regime filter.
  1. 5m RSI(7) from Lighter REST API
  2. Entry: RSI in zone (< 35 or > 65) + RSI turning + VWAP σ band + ADX < 30
  3. Signal debouncing (2 consecutive confirmations)
  4. Session filter (skip 00:00-03:59 UTC)
  5. ATR-based dynamic SL and trailing stop
  6. OB imbalance conviction filter
  7. RSI profit exit (opposite zone + profit > 0)

Safety: SL always on exchange, position sync, no naked positions, no double orders
"""
import asyncio, time, requests, lighter, threading, json, collections, os, traceback, urllib.request, calendar, math
from datetime import datetime, timezone
from lighter.signer_client import CreateOrderTxReq
import websocket as ws_lib

# ══════════════════════════════════════════════════════════════════════════
# MODE
# ══════════════════════════════════════════════════════════════════════════
PAPER_TRADE = False
PAPER_STARTING_BALANCE = 71.30

# ── VERSION ──────────────────────────────────────────────────────────────
BOT_VERSION = "v11.1"          # bump this on every meaningful change

# ── CONFIG ───────────────────────────────────────────────────────────────
ACCOUNT_INDEX=716892; API_KEY_INDEX=3
API_PRIVATE_KEY = os.environ.get("LIGHTER_API_KEY", "")
LIGHTER_URL="https://mainnet.zklighter.elliot.ai"; MARKET_INDEX=1
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "5583279698")

BASE_AMOUNT = 150  # 0.0015 BTC (~$100 notional, ~$2 margin at 50x)

# ── 5m RSI STRATEGY (from Lighter REST API) ──────────────────────────────
RSI_PERIOD = 7                 # faster response for 5m scalping
LIGHTER_CANDLE_URL = "https://mainnet.zklighter.elliot.ai/api/v1/candles"
CANDLE_FETCH_COUNT = 200       # 200 candles — plenty for RSI + VWAP

# Entry zones: tighter — true oversold/overbought
RSI_LONG_MAX = 42              # long only on deep oversold
RSI_SHORT_MIN = 65             # short when RSI > 65
EMA_OVERRIDE_LONG = 30          # RSI below this bypasses EMA block for longs
EMA_OVERRIDE_SHORT = 75         # RSI above this bypasses EMA block for shorts

# RSI turning: confirms reversal from trough/peak
RSI_TURN_DELTA = 2.0           # min RSI rise from trough (long) or fall from peak (short)
RSI_TURN_WINDOW = 120          # seconds to look back for trough/peak

# RSI-based exits — profit only
RSI_LONG_PROFIT_EXIT = 65      # take profit when RSI reaches overbought-ish
RSI_SHORT_PROFIT_EXIT = 35     # take profit when RSI reaches oversold-ish

# Volume spike — relaxes RSI zones during high-volume moves
VOL_SPIKE_WINDOW = 60          # seconds to measure recent volume
VOL_SPIKE_LOOKBACK = 600       # seconds for average volume baseline
VOL_SPIKE_MULTIPLIER = 3.0     # recent must be 3x average to count as spike
VOL_SPIKE_ZONE_RELAX = 2       # relax RSI zones by 5 pts during spike (35/65 → 40/60)
VOL_BIG_SPIKE_MULTIPLIER = 5.0 # 5x average = big spike (uses same relaxed zones, no bypass)

# ── SESSION FILTER ───────────────────────────────────────────────────────
SESSION_FILTER_ENABLED = False
SESSION_BLOCKED_HOURS_UTC = (0, 1, 2, 3)  # skip 00:00-03:59 UTC (thin Asian liquidity)

# ── ADX REGIME FILTER (15m) ──────────────────────────────────────────────
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 40       # ADX > 40 = strong trend, skip mean-reversion
ADX_TIMEFRAME = "15m"
ADX_POLL_INTERVAL = 60         # fetch 15m candles every 60s (they change slowly)

# ── VWAP SIGMA BANDS ─────────────────────────────────────────────────────
VWAP_SIGMA_ENTRY = 0.75        # require price at VWAP ± 0.75σ for entry

# ── TREND FILTER (EMA on 5m) ────────────────────────────────────────────
TREND_EMA_PERIOD = 20           # EMA(20) on 5m closes — skip longs below, shorts unfiltered

# ── OB IMBALANCE FILTER ──────────────────────────────────────────────────
IMB_FILTER_ENABLED = False
IMB_LONG_MIN = 0.05            # imb_ema must be > 0.05 for longs (mild bid pressure)
IMB_SHORT_MAX = -0.05          # imb_ema must be < -0.05 for shorts (mild ask pressure)

# RSI poll interval — fetch fresh 5m candles from Lighter API
RSI_POLL_INTERVAL = 20         # seconds between Lighter API candle fetches

# ── ATR-BASED EXITS ──────────────────────────────────────────────────────
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.2        # SL = 1.2x ATR
ATR_TP_MULTIPLIER = 2.5        # TP target = 2.5x ATR (for R:R calc, trail handles actual exit)
ATR_TRAIL_ACTIVATE_MULT = 0.3  # activate trail after 0.3x ATR profit (was 0.5)
ATR_TRAIL_DISTANCE_MULT = 0.4  # trail distance = 0.4x ATR (was 0.5)
ATR_MIN_SL_PCT = 0.0015        # floor: never tighter than 0.15%
ATR_MAX_SL_PCT = 0.0035        # ceiling: never wider than 0.35% (was 0.60%)
HARD_STOP_PCT = 0.0035         # hard backstop 0.35% (was 0.45%)
MAX_HOLD_SECS = 210            # 3.5 min normal (was 300)
TRAIL_MAX_HOLD_SECS = 330      # 5.5 min trailing (was 450)
LOCK_DURATION = 450

# ── COOLDOWNS ────────────────────────────────────────────────────────────
MIN_TRADE_INTERVAL = 45        # 45s between trades
LOSS_COOLDOWN_SECS = 60        # pause after loss
LOSS_STREAK_COOLDOWN = 600     # 10 min after 2 losses (was 300s after 3)
MAX_TRADES_HOUR = 8            # reduced from 15 (tighter filters = fewer, better trades)
DAILY_LOSS_LIMIT = 2.0

# ── KEPT FOR OB LOGGING ─────────────────────────────────────────────────
IMB_LEVELS = 5; IMB_EMA_ALPHA = 0.1
SMOOTH_TICKS = 10; LOOKBACK_TICKS = 30

EXIT_SETTLE_SECS = 5

STATE_FILE="/root/rsi_bot/.bot_state_v11.json"
LOCK_FILE="/root/rsi_bot/.entry_lock_v11"
LOCAL_POS_FILE="/root/rsi_bot/.local_position_v11.json"
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
acct_ws_pos_updated_at = 0.0  # timestamp of last WS position update

tape_lock = threading.Lock()
tape_recent = collections.deque(maxlen=500)
tape_whale_threshold = 0.5
tape_last_whale = None
_hard_stop_tick_count = 0

# 5m RSI state from Lighter API
rsi_5m_current = None
ema_5m_current = None
rsi_5m_history = collections.deque(maxlen=60)
vwap_current = None
vwap_upper_band = None         # VWAP + 1σ
vwap_lower_band = None         # VWAP - 1σ
rsi_lock = threading.Lock()
last_rsi_fetch = 0
last_rest_pos_verify = 0
REST_POS_VERIFY_INTERVAL = 60  # verify exchange position via REST every 60s

# ATR state
atr_5m_current = None
atr_lock = threading.Lock()

# ADX regime state
adx_current = None
adx_lock = threading.Lock()
last_adx_fetch = 0

# Signal debounce state
signal_debounce = {"direction": None, "count": 0, "price": 0.0}
SIGNAL_DEBOUNCE_REQUIRED = 1   # must see signal N consecutive times

bot_start_time = time.time()
trade_entry_context = {}


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
               'trailing_active','best_price','trail_stop_price',
               'sl_distance_pct','trail_activate_pct','trail_distance_pct']

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

        # ATR-based stop loss
        with atr_lock:
            atr = atr_5m_current

        if atr is not None and entry_price > 0:
            sl_distance_pct = (atr * ATR_SL_MULTIPLIER) / entry_price
            sl_distance_pct = max(ATR_MIN_SL_PCT, min(ATR_MAX_SL_PCT, sl_distance_pct))
            trail_activate_pct = (atr * ATR_TRAIL_ACTIVATE_MULT) / entry_price
            trail_distance_pct = (atr * ATR_TRAIL_DISTANCE_MULT) / entry_price
        else:
            # Fallback to reasonable defaults if ATR unavailable
            sl_distance_pct = 0.0040
            trail_activate_pct = 0.0030
            trail_distance_pct = 0.0015

        self.sl_distance_pct = sl_distance_pct
        self.trail_activate_pct = trail_activate_pct
        self.trail_distance_pct = trail_distance_pct

        if side == 'long':
            self.sl_price = entry_price * (1 - sl_distance_pct)
            self.tp_price = entry_price * (1 + 0.005)
        else:
            self.sl_price = entry_price * (1 + sl_distance_pct)
            self.tp_price = entry_price * (1 - 0.005)
        self._save()
        print(f"  OPENED {side} @ ${entry_price:,.1f} SL=${self.sl_price:,.1f} (ATR-SL:{sl_distance_pct*100:.2f}%)")

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

    def update_trailing_stop(self, current_price):
        """ATR-based trailing stop. Activate and trail distances set at entry from ATR."""
        if not self.in_position: return None

        activate_pct = getattr(self, 'trail_activate_pct', 0.0030)
        distance_pct = getattr(self, 'trail_distance_pct', 0.0015)

        profit_pct = self.unrealized_pct(current_price)

        if not self.trailing_active and profit_pct >= activate_pct:
            self.trailing_active = True
            self.best_price = current_price
            if self.side == 'long':
                self.trail_stop_price = current_price * (1 - distance_pct)
            else:
                self.trail_stop_price = current_price * (1 + distance_pct)
            self._save()
            print(f"  TRAIL ACTIVATED at +{profit_pct*100:.3f}% | stop=${self.trail_stop_price:,.1f}")
            return None

        if not self.trailing_active: return None

        if self.side == 'long':
            if current_price > self.best_price:
                self.best_price = current_price
                self.trail_stop_price = current_price * (1 - distance_pct)
            if current_price <= self.trail_stop_price:
                return 'trail_exit'
        else:
            if current_price < self.best_price:
                self.best_price = current_price
                self.trail_stop_price = current_price * (1 + distance_pct)
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
# WS FEED (orderbook for price + account for fills + trade tape)
# ══════════════════════════════════════════════════════════════════════════
def ws_thread():
    global ob_bids, ob_asks, imb_ema
    global acct_ws_position, acct_ws_balance, acct_ws_last_trade, acct_ws_pos_updated_at
    global tape_last_whale

    def on_msg(ws, msg):
        global ob_bids, ob_asks, imb_ema
        global acct_ws_position, acct_ws_balance, acct_ws_last_trade, acct_ws_pos_updated_at
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
                            acct_ws_pos_updated_at = time.time()
                        else:
                            acct_ws_position = {"size": 0.0, "sign": 0, "entry": 0.0, "open_orders": 0}
                            acct_ws_pos_updated_at = time.time()
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
                            if size >= 25.0:
                                _usd = trade_info['usd']
                                _usd_str = f"${_usd/1_000_000:.1f}M" if _usd >= 1_000_000 else f"${_usd/1000:.0f}K"
                                _dir = "🔴 SELL" if side.lower() in ("ask", "sell") else "🟢 BUY"
                                print(f"  WHALE {side.upper()} {size:.2f} BTC ({_usd_str}) @ ${price:,.1f}")
                                tg(f"🐋 WHALE {_dir} {size:.1f} BTC ({_usd_str})\n@ ${price:,.0f}")
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

    def on_close(ws, *a):
        global acct_ws_pos_updated_at
        print("  WS disconnected — marking position data stale")
        acct_ws_ready.clear()
        with acct_ws_lock:
            acct_ws_pos_updated_at = 0.0  # mark stale
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
# LIGHTER REST API — RSI, ADX, ATR FROM CANDLES
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


def compute_adx(candles, period=14):
    """Compute ADX from OHLCV candles using Wilder's smoothing."""
    if len(candles) < period * 2 + 1:
        return None

    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, len(candles)):
        h = candles[i]["h"]
        l = candles[i]["l"]
        c_prev = candles[i-1]["c"]
        h_prev = candles[i-1]["h"]
        l_prev = candles[i-1]["l"]

        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)

        up_move = h - h_prev
        down_move = l_prev - l
        plus_dm_list.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm_list.append(down_move if down_move > up_move and down_move > 0 else 0)

    if len(tr_list) < period:
        return None

    atr = sum(tr_list[:period]) / period
    plus_dm_smooth = sum(plus_dm_list[:period]) / period
    minus_dm_smooth = sum(minus_dm_list[:period]) / period

    dx_list = []
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        plus_dm_smooth = (plus_dm_smooth * (period - 1) + plus_dm_list[i]) / period
        minus_dm_smooth = (minus_dm_smooth * (period - 1) + minus_dm_list[i]) / period

        if atr == 0:
            continue
        plus_di = 100 * plus_dm_smooth / atr
        minus_di = 100 * minus_dm_smooth / atr
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_list.append(0)
        else:
            dx_list.append(100 * abs(plus_di - minus_di) / di_sum)

    if len(dx_list) < period:
        return None

    adx = sum(dx_list[:period]) / period
    for i in range(period, len(dx_list)):
        adx = (adx * (period - 1) + dx_list[i]) / period

    return adx


def compute_atr(candles, period=14):
    """Compute ATR from OHLCV candles using Wilder's smoothing."""
    if len(candles) < period + 1:
        return None

    tr_list = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]
        l = candles[i]["l"]
        c_prev = candles[i-1]["c"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)

    if len(tr_list) < period:
        return None

    atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period

    return atr


def fetch_candles_from_lighter(resolution, count=CANDLE_FETCH_COUNT):
    """Fetch candles from Lighter REST API. Returns list of close prices."""
    try:
        now = int(time.time())
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
    """Compute VWAP and σ bands from today's candles. Resets at midnight UTC."""
    now_utc = datetime.now(timezone.utc)
    today_start = int(calendar.timegm(now_utc.replace(hour=0, minute=0, second=0, microsecond=0).timetuple()))
    today_candles = [c for c in candles if c["t"] >= today_start]

    cum_tpv = 0.0
    cum_vol = 0.0
    cum_tp2v = 0.0

    for c in today_candles:
        tp = (c["h"] + c["l"] + c["c"]) / 3
        cum_tpv += tp * c["v"]
        cum_tp2v += (tp ** 2) * c["v"]
        cum_vol += c["v"]

    if cum_vol == 0:
        return None, None, None

    vwap = cum_tpv / cum_vol
    variance = (cum_tp2v / cum_vol) - (vwap ** 2)
    sigma = variance ** 0.5 if variance > 0 else 0

    upper_band = vwap + VWAP_SIGMA_ENTRY * sigma
    lower_band = vwap - VWAP_SIGMA_ENTRY * sigma

    return vwap, upper_band, lower_band


def compute_ema_from_closes(closes, period):
    """Compute EMA from a list of close prices."""
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # SMA seed
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def fetch_rsi_from_lighter():
    """Fetch 5m RSI + VWAP + ATR + EMA from Lighter REST API. Called every 20s."""
    global rsi_5m_current, vwap_current, vwap_upper_band, vwap_lower_band, last_rsi_fetch
    global atr_5m_current, ema_5m_current

    ohlcv_5m = fetch_candles_ohlcv("5m", CANDLE_FETCH_COUNT)
    if ohlcv_5m and len(ohlcv_5m) >= RSI_PERIOD + 2:
        closes_5m = [c["c"] for c in ohlcv_5m]
        new_rsi_5m = compute_rsi(closes_5m, RSI_PERIOD)
        new_vwap, new_upper, new_lower = compute_vwap(ohlcv_5m)
        new_ema = compute_ema_from_closes(closes_5m, TREND_EMA_PERIOD)
        with rsi_lock:
            rsi_5m_current = new_rsi_5m
            vwap_current = new_vwap
            vwap_upper_band = new_upper
            vwap_lower_band = new_lower
            ema_5m_current = new_ema
            if new_rsi_5m is not None:
                rsi_5m_history.append((time.time(), new_rsi_5m))
        if new_rsi_5m is not None:
            ema_str = f" EMA=${new_ema:,.0f}" if new_ema else ""
            print(f"  5m RSI: {new_rsi_5m:.1f}{ema_str} [{len(closes_5m)} candles]")

        # Compute ATR from same 5m candles
        new_atr = compute_atr(ohlcv_5m, ATR_PERIOD)
        with atr_lock:
            atr_5m_current = new_atr
        if new_atr is not None:
            print(f"  5m ATR: ${new_atr:,.1f}")

    last_rsi_fetch = time.time()


def fetch_adx():
    """Fetch 15m candles and compute ADX. Called every 60s."""
    global adx_current, last_adx_fetch

    ohlcv_15m = fetch_candles_ohlcv(ADX_TIMEFRAME, 100)
    if ohlcv_15m and len(ohlcv_15m) >= ADX_PERIOD * 2 + 2:
        new_adx = compute_adx(ohlcv_15m, ADX_PERIOD)
        with adx_lock:
            adx_current = new_adx
        if new_adx is not None:
            regime = "TREND" if new_adx > ADX_TREND_THRESHOLD else "RANGE"
            print(f"  15m ADX: {new_adx:.1f} ({regime})")

    last_adx_fetch = time.time()


# ══════════════════════════════════════════════════════════════════════════
# RSI HELPERS
# ══════════════════════════════════════════════════════════════════════════
def rsi_is_turning(direction):
    """
    Check if RSI is turning in our direction using trough/peak detection.
    Returns (is_turning, current_rsi, extreme_rsi) or (False, rsi, None).
    """
    with rsi_lock:
        rsi_now = rsi_5m_current
        history = list(rsi_5m_history)

    if rsi_now is None or len(history) < 2:
        return False, rsi_now, None

    cutoff = time.time() - RSI_TURN_WINDOW
    window = [r for t, r in history if t >= cutoff]
    if not window:
        return False, rsi_now, None

    if direction == 'long':
        trough = min(window)
        delta = rsi_now - trough
        return delta >= RSI_TURN_DELTA, rsi_now, trough
    else:
        peak = max(window)
        delta = peak - rsi_now
        return delta >= RSI_TURN_DELTA, rsi_now, peak


# ══════════════════════════════════════════════════════════════════════════
# VOLUME SPIKE DETECTION
# ══════════════════════════════════════════════════════════════════════════
def detect_volume_spike():
    """
    Detect unusual volume in the last 60s vs 10-min rolling average.
    Returns (spike_level, recent_vol, avg_vol).
    spike_level: 0=none, 1=normal spike (3x), 2=big spike (5x)
    """
    with tape_lock:
        trades = list(tape_recent)
    if not trades:
        return 0, 0, 0

    now = time.time()
    recent_vol = sum(t["size"] for t in trades if t["time"] > now - VOL_SPIKE_WINDOW)
    lookback_vol = sum(t["size"] for t in trades if t["time"] > now - VOL_SPIKE_LOOKBACK)

    avg_vol = (lookback_vol / VOL_SPIKE_LOOKBACK) * VOL_SPIKE_WINDOW if lookback_vol > 0 else 0

    if avg_vol > 0 and recent_vol >= avg_vol * VOL_BIG_SPIKE_MULTIPLIER:
        return 2, recent_vol, avg_vol
    if avg_vol > 0 and recent_vol >= avg_vol * VOL_SPIKE_MULTIPLIER:
        return 1, recent_vol, avg_vol
    return 0, recent_vol, avg_vol


# ══════════════════════════════════════════════════════════════════════════
# ENTRY EVALUATION — RSI zone + turning + VWAP σ + ADX + OB imbalance
# ══════════════════════════════════════════════════════════════════════════
def evaluate_entry(price):
    """
    Entry: RSI zone (35/65) + turning + VWAP σ bands + ADX regime filter.
    Volume spike relaxes RSI zones by 5 pts (35/65 → 40/60).
    OB imbalance conviction filter.
    Returns (direction, rsi_5m, vol_spike) or (None, rsi_5m, False)
    """
    with rsi_lock:
        rsi_5m = rsi_5m_current
        vwap = vwap_current

    if rsi_5m is None:
        return None, rsi_5m, False

    # Session filter — skip low-liquidity hours
    if SESSION_FILTER_ENABLED:
        current_hour_utc = datetime.now(timezone.utc).hour
        if current_hour_utc in SESSION_BLOCKED_HOURS_UTC:
            return None, rsi_5m, False

    # EMA trend filter — block counter-trend entries (always active)
    ema_block_long = False
    ema_block_short = False
    with rsi_lock:
        ema = ema_5m_current
    if ema is not None:
        if price > ema:
            ema_block_short = True   # uptrend: don’t short
        elif price < ema:
            ema_block_long = True    # downtrend: don’t long

    # Volume spike detection — relaxes RSI zones
    spike_level, recent_vol, avg_vol = detect_volume_spike()
    is_spike = spike_level > 0
    is_big_spike = spike_level >= 2

    if is_big_spike:
        print(f"  BIG VOL SPIKE: {recent_vol:.2f} BTC in {VOL_SPIKE_WINDOW}s ({recent_vol/avg_vol:.1f}x avg) — zones relaxed to {RSI_LONG_MAX+VOL_SPIKE_ZONE_RELAX}/{RSI_SHORT_MIN-VOL_SPIKE_ZONE_RELAX}")
    elif is_spike:
        print(f"  VOL SPIKE: {recent_vol:.2f} BTC in {VOL_SPIKE_WINDOW}s ({recent_vol/avg_vol:.1f}x avg) — zones relaxed to {RSI_LONG_MAX+VOL_SPIKE_ZONE_RELAX}/{RSI_SHORT_MIN-VOL_SPIKE_ZONE_RELAX}")

    # OB imbalance conviction filter
    if IMB_FILTER_ENABLED:
        current_imb = get_imb_ema()

    direction = None

    # ── LONG ──
    long_zone_ok = rsi_5m < (RSI_LONG_MAX + VOL_SPIKE_ZONE_RELAX if is_spike or is_big_spike else RSI_LONG_MAX)
    if long_zone_ok and ema_block_long:
        if rsi_5m < EMA_OVERRIDE_LONG:
            print(f"  EMA override: RSI {rsi_5m:.1f} < {EMA_OVERRIDE_LONG} — allowing LONG despite price ${price:,.0f} < EMA ${ema:,.0f}")
        else:
            print(f"  LONG blocked: price ${price:,.0f} < EMA({TREND_EMA_PERIOD}) ${ema:,.0f}")
            long_zone_ok = False

    if long_zone_ok:
        turning, _, trough = rsi_is_turning('long')
        if turning:
            direction = 'long'
            trough_str = f" trough={trough:.1f}" if trough is not None else ""
            spike_str = " +BIGVOL" if is_big_spike else " +VOL" if is_spike else ""
            print(f"  SIGNAL: LONG{spike_str} | RSI={rsi_5m:.1f}{trough_str} | price=${price:,.0f}")

    # ── SHORT ──
    short_zone_ok = rsi_5m > (RSI_SHORT_MIN - VOL_SPIKE_ZONE_RELAX if is_spike or is_big_spike else RSI_SHORT_MIN)
    if direction is None and short_zone_ok and ema_block_short:
        if rsi_5m > EMA_OVERRIDE_SHORT:
            print(f"  EMA override: RSI {rsi_5m:.1f} > {EMA_OVERRIDE_SHORT} — allowing SHORT despite price ${price:,.0f} > EMA ${ema:,.0f}")
        else:
            print(f"  SHORT blocked: price ${price:,.0f} > EMA({TREND_EMA_PERIOD}) ${ema:,.0f}")
            short_zone_ok = False

    if direction is None and short_zone_ok:
        turning, _, peak = rsi_is_turning('short')
        if turning:
            direction = 'short'
            peak_str = f" peak={peak:.1f}" if peak is not None else ""
            spike_str = " +BIGVOL" if is_big_spike else " +VOL" if is_spike else ""
            print(f"  SIGNAL: SHORT{spike_str} | RSI={rsi_5m:.1f}{peak_str} | price=${price:,.0f}")

    return direction, rsi_5m, is_spike


# ══════════════════════════════════════════════════════════════════════════
# RSI EXIT CHECK — profit only
# ══════════════════════════════════════════════════════════════════════════
def check_rsi_exit(local_pos, price):
    """RSI profit exit only."""
    if not local_pos.in_position:
        return False, ""

    with rsi_lock:
        rsi_5m = rsi_5m_current

    if rsi_5m is None:
        return False, ""

    profit_pct = local_pos.unrealized_pct(price)

    if local_pos.side == 'long':
        if rsi_5m >= RSI_LONG_PROFIT_EXIT and profit_pct > 0:
            return True, "rsi_profit"
    else:
        if rsi_5m <= RSI_SHORT_PROFIT_EXIT and profit_pct > 0:
            return True, "rsi_profit"

    return False, ""


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
WS_STALE_SECS = 30  # if no WS position update in 30s, data is stale

def ws_get_position():
    with acct_ws_lock: return dict(acct_ws_position)

def ws_pos_is_stale():
    with acct_ws_lock: return acct_ws_pos_updated_at == 0.0 or (time.time() - acct_ws_pos_updated_at > WS_STALE_SECS)

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
    stale = ws_pos_is_stale()

    # If WS data is stale AND we think we're in a position, verify via REST
    if stale and local_pos.in_position:
        print(f"  SYNC: WS data stale — verifying position via REST")
        rest_pos = api_get_position()
        if rest_pos:
            if rest_pos["size"] == 0:
                # Exchange is flat but local thinks we're in a position — phantom!
                _s = local_pos.side
                _e = local_pos.entry_price
                exit_p = local_pos.sl_price if local_pos.sl_price else _e
                pnl_usd, pnl_pct = calc_pnl_from_prices(_s, _e, exit_p)
                print(f"  SYNC: Phantom {_s} cleared — exchange flat (WS was stale)")
                _x = "🟢" if pnl_usd >= 0 else "🔴"
                tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | SL (stale WS)\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                handle_exit_pnl(_s, _e, exit_p, "sl_exchange")
                local_pos.close("sync_stale_ws")
                do_save_state()
                return True
            else:
                # Exchange has position, update WS state from REST to un-stale it
                with acct_ws_lock:
                    global acct_ws_pos_updated_at
                    acct_ws_position.update({"size": rest_pos["size"], "sign": rest_pos["sign"], "entry": rest_pos["entry"], "open_orders": rest_pos["open_orders"]})
                    acct_ws_pos_updated_at = time.time()
        return False

    # CASE A: Local flat but exchange has position (naked/orphan)
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

    # CASE B: Local says in_position but WS says flat — confirm with REST
    if local_pos.in_position and ws_pos["size"] == 0:
        rest_pos = api_get_position()
        if rest_pos and rest_pos["size"] == 0:
            _s = local_pos.side
            _e = local_pos.entry_price
            exit_p = local_pos.sl_price if local_pos.sl_price else _e
            pnl_usd, pnl_pct = calc_pnl_from_prices(_s, _e, exit_p)
            print(f"  SYNC: Bot thinks {_s} but exchange flat — SL likely filled")
            _x = "🟢" if pnl_usd >= 0 else "🔴"
            tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | SL\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
            handle_exit_pnl(_s, _e, exit_p, "sl_exchange")
            local_pos.close("sync_flat")
            do_save_state()
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
         "last_day": time.strftime("%d"), "consecutive_losses": 0, "last_loss_time": 0,
         "ver": BOT_VERSION, "ver_pnl": 0.0, "ver_trades": 0, "ver_wins": 0, "ver_losses": 0,
         "ver_started": time.strftime("%Y-%m-%d %H:%M")}
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
        sl_dist = getattr(local_pos, 'sl_distance_pct', 0.0040)
        if is_long:
            sl_p = entry * (1 - sl_dist)
            ea = 1
            sl_l = int(sl_p * 0.98 * 10)
        else:
            sl_p = entry * (1 + sl_dist)
            ea = 0
            sl_l = int(sl_p * 1.02 * 10)
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
trades_this_hour = 0; hour_reset_time = time.time()
last_trade_time = state["last_trade_time"]
last_update_id = 0; paused = False; last_day = state["last_day"]
last_direction = state["last_direction"]
long_pnl = 0.0; short_pnl = 0.0; long_trades = 0; short_trades = 0

# Restore counters from state (survive restarts)
if last_day == time.strftime("%d"):
    daily_pnl = state["daily_pnl"]; daily_trades = state["daily_trades"]
    wins = state["wins"]; losses = state["losses"]
    consecutive_losses = state["consecutive_losses"]
    last_loss_time = state["last_loss_time"]
else:
    daily_pnl = 0.0; daily_trades = 0; wins = 0; losses = 0
    consecutive_losses = 0; last_loss_time = 0

# Version-level tracking — reset on version change
if state["ver"] == BOT_VERSION:
    ver_pnl = state["ver_pnl"]; ver_trades = state["ver_trades"]
    ver_wins = state["ver_wins"]; ver_losses = state["ver_losses"]
    ver_started = state["ver_started"]
else:
    print(f"  Version changed: {state['ver']} → {BOT_VERSION} — resetting baseline")
    ver_pnl = 0.0; ver_trades = 0; ver_wins = 0; ver_losses = 0
    ver_started = time.strftime("%Y-%m-%d %H:%M")

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
                    r5m = rsi_5m_current; vwap = vwap_current
                with adx_lock:
                    adx_val = adx_current
                with atr_lock:
                    atr_val = atr_5m_current

                pos_str = f"{local_pos.side} @ ${local_pos.entry_price:,.0f}" if local_pos.in_position else "flat"
                sl_str = " SL:ON" if local_pos.oco_attached else " SL:LOCAL" if local_pos.in_position else ""
                mode = "PAPER" if PAPER_TRADE else "LIVE"
                wr = f"{wins/(wins+losses)*100:.0f}%" if wins+losses > 0 else "N/A"

                r5m_str = f"{r5m:.1f}" if r5m is not None else "n/a"
                vwap_s = f"${vwap:,.0f}" if vwap is not None else "n/a"
                adx_s = f"{adx_val:.0f}" if adx_val is not None else "n/a"
                atr_s = f"${atr_val:,.1f}" if atr_val is not None else "n/a"

                pnl_str = ""
                if local_pos.in_position and price:
                    pnl_str = f"\nUnreal: {local_pos.unrealized_pct(price)*100:+.3f}% hold:{local_pos.hold_time()}s"

                ver_wr = f"{ver_wins/(ver_wins+ver_losses)*100:.0f}%" if ver_wins+ver_losses > 0 else "N/A"
                tg(f"{BOT_VERSION} RSI({RSI_PERIOD})+VWAPσ+ADX [{mode}]\n${price:,.0f} | Imb:{ie:+.2f}\n"
                   f"RSI 5m:{r5m_str} | ADX:{adx_s} | ATR:{atr_s}\n"
                   f"VWAP: {vwap_s}\n"
                   f"Pos: {pos_str}{sl_str}{pnl_str}\n"
                   f"Day: ${daily_pnl:.2f} | {wins}W/{losses}L ({wr})\n"
                   f"{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t {ver_wins}W/{ver_losses}L ({ver_wr})\n"
                   f"Since: {ver_started}\n"
                   f"Streak: {consecutive_losses}L | HS:{HARD_STOP_PCT*100:.2f}%")
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

    with atr_lock:
        atr_val = atr_5m_current
    with adx_lock:
        adx_val = adx_current

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
        "vwap": ctx.get("vwap"),
        "vol_spike": ctx.get("vol_spike", False),
        "atr_5m": round(atr_val, 2) if atr_val else None,
        "adx_15m": round(adx_val, 1) if adx_val else None,
        "sl_pct": round(getattr(local_pos, 'sl_distance_pct', 0) * 100, 3),
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
    global last_trade_time, ver_pnl, ver_trades, ver_wins, ver_losses

    hold_secs = local_pos.hold_time()

    if is_paper and paper:
        pnl_usd, pnl_pct, fill = paper.execute_exit(side, entry, exit_p, reason)
        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses
        daily_trades = paper.total_trades
        if pnl_usd >= 0: consecutive_losses = 0
        else: consecutive_losses += 1; last_loss_time = time.time()
        ver_pnl += pnl_usd; ver_trades += 1
        if pnl_usd >= 0: ver_wins += 1
        else: ver_losses += 1
        log_trade(side, entry, exit_p, reason, pnl_usd, pnl_pct, hold_secs)
        return pnl_usd, pnl_pct
    else:
        pnl_usd, pnl_pct = calc_pnl_from_prices(side, entry, exit_p)
        daily_pnl += pnl_usd; daily_trades += 1
        track_direction_pnl(side, pnl_usd)
        if pnl_usd >= 0: wins += 1; consecutive_losses = 0
        else: losses += 1; consecutive_losses += 1; last_loss_time = time.time()
        ver_pnl += pnl_usd; ver_trades += 1
        if pnl_usd >= 0: ver_wins += 1
        else: ver_losses += 1
        log_trade(side, entry, exit_p, reason, pnl_usd, pnl_pct, hold_secs)
        return pnl_usd, pnl_pct


def do_save_state():
    save_state({"last_trade_time": last_trade_time, "last_direction": last_direction,
        "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
        "daily_trades": daily_trades, "last_day": last_day,
        "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time,
        "ver": BOT_VERSION, "ver_pnl": ver_pnl, "ver_trades": ver_trades,
        "ver_wins": ver_wins, "ver_losses": ver_losses, "ver_started": ver_started})


# ══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════
async def main():
    global daily_pnl, daily_trades, trades_this_hour, hour_reset_time, last_trade_time
    global wins, losses, last_day, paused, last_direction
    global consecutive_losses, last_loss_time, bot_start_time
    global last_rsi_fetch, last_adx_fetch, last_rest_pos_verify
    global long_pnl, short_pnl, long_trades, short_trades
    global signal_debounce
    global ver_pnl, ver_trades, ver_wins, ver_losses

    mode_str = "PAPER" if PAPER_TRADE else "LIVE"
    print(f"RSI Bot v11 — Mean-Reversion Scalper (RSI({RSI_PERIOD}) + VWAP σ + ADX) [{mode_str}]")
    print(f"  Strategy: RSI zone + turning + VWAP σ bands + ADX regime + OB imbalance")
    print(f"  Entry: Long RSI<{RSI_LONG_MAX} turning | Short RSI>{RSI_SHORT_MIN} turning")
    print(f"  Turn: Δ{RSI_TURN_DELTA} from {RSI_TURN_WINDOW}s trough/peak")
    print(f"  VWAP: long ≤ VWAP-{VWAP_SIGMA_ENTRY}σ | short ≥ VWAP+{VWAP_SIGMA_ENTRY}σ")
    print(f"  ADX filter: skip when ADX>{ADX_TREND_THRESHOLD} (trending)")
    print(f"  Session: skip {SESSION_BLOCKED_HOURS_UTC} UTC")
    print(f"  Debounce: {SIGNAL_DEBOUNCE_REQUIRED}x consecutive")
    print(f"  Size: {BASE_AMOUNT/100000:.4f} BTC | ATR stops ({ATR_SL_MULTIPLIER}x SL, {ATR_TRAIL_DISTANCE_MULT}x trail)")
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

    # Bootstrap RSI + VWAP + ATR from Lighter API
    print("  Fetching initial RSI + VWAP + ATR from Lighter API...")
    fetch_rsi_from_lighter()
    with rsi_lock:
        r5m_boot = rsi_5m_current; vwap_boot = vwap_current; ema_boot = ema_5m_current
    with atr_lock:
        atr_boot = atr_5m_current
    r5m_str = f"{r5m_boot:.1f}" if r5m_boot else "--"
    ema_str = f"${ema_boot:,.0f}" if ema_boot else "--"
    atr_str = f"${atr_boot:,.1f}" if atr_boot else "--"
    print(f"  Bootstrap: 5m RSI={r5m_str} EMA(20)={ema_str} ATR={atr_str}")

    # Bootstrap ADX
    print("  Fetching initial ADX from Lighter API...")
    fetch_adx()
    with adx_lock:
        adx_boot = adx_current
    adx_str = f"{adx_boot:.1f}" if adx_boot else "--"
    print(f"  Bootstrap: 15m ADX={adx_str}")

    unlock_entry()

    starting_bal = None
    if not PAPER_TRADE:
        starting_bal = await startup_close_orphan(signer)
        if starting_bal is None:
            tg("Bot aborted -- could not close orphan."); return

    if local_pos.in_position:
        print(f"  Stale local position -- clearing")
        local_pos.close("stale_startup")

    tg(f"v11 RSI({RSI_PERIOD}) + ADX trend filter [{mode_str}]\n"
       f"Entry: RSI<{RSI_LONG_MAX}/>={RSI_SHORT_MIN} | ADX>{ADX_TREND_THRESHOLD} blocks counter-trend\n"
       f"Session: skip {SESSION_BLOCKED_HOURS_UTC}\n"
       f"Stops: ATR-based ({ATR_SL_MULTIPLIER}x SL, {ATR_TRAIL_DISTANCE_MULT}x trail)\n"
       f"RSI: 5m={r5m_str} EMA(20)={ema_str} ADX={adx_str}"
       + (f"\nPaper: ${paper.balance:.2f}" if PAPER_TRADE else f"\nBalance: ${starting_bal:.2f}"))

    last_tg_check = 0

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

            # Fetch fresh RSI from Lighter API every 20s
            rsi_refreshed = False
            if now - last_rsi_fetch >= RSI_POLL_INTERVAL:
                fetch_rsi_from_lighter()
                rsi_refreshed = True

            # Fetch fresh ADX from Lighter API every 60s
            if now - last_adx_fetch >= ADX_POLL_INTERVAL:
                fetch_adx()

            # Position sync
            if not PAPER_TRADE and signer:
                sync_handled = await position_sync_check(signer, local_pos)
                if sync_handled: await asyncio.sleep(3); continue

            # Periodic REST position verify (catches stale WS even when it looks fresh)
            if not PAPER_TRADE and local_pos.in_position and now - last_rest_pos_verify >= REST_POS_VERIFY_INTERVAL:
                last_rest_pos_verify = now
                rest_pos = api_get_position()
                if rest_pos and rest_pos["size"] == 0:
                    _s = local_pos.side
                    _e = local_pos.entry_price
                    exit_p = local_pos.sl_price if local_pos.sl_price else _e
                    pnl_usd, pnl_pct = calc_pnl_from_prices(_s, _e, exit_p)
                    print(f"  REST VERIFY: Phantom {_s} — exchange flat! Clearing.")
                    _x = "🟢" if pnl_usd >= 0 else "🔴"
                    tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | SL (REST verify)\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                    handle_exit_pnl(_s, _e, exit_p, "sl_exchange")
                    local_pos.close("rest_verify_flat")
                    do_save_state()
                    await asyncio.sleep(3); continue

            locked = is_entry_locked()
            cd = max(0, int(last_trade_time + MIN_TRADE_INTERVAL - now))
            hold = local_pos.hold_time()
            ie = get_imb_ema()

            with rsi_lock:
                r5m = rsi_5m_current; vwap = vwap_current; ema = ema_5m_current
            with adx_lock:
                adx_disp = adx_current
            r5m_str = f"r5m={r5m:.1f}" if r5m is not None else "r5m=--"
            ema_str = f"ema=${ema:,.0f}" if ema is not None else "ema=--"
            pos_str = f"{local_pos.side[0].upper()}" if local_pos.in_position else "-"
            sl_tag = "SL" if local_pos.oco_attached else "L" if local_pos.in_position else ""
            lk = "LOCK" if locked else ""
            trend_str = ""
            if ema is not None:
                trend_str = "↑" if price >= ema else "↓"

            print(f"[{time.strftime('%H:%M:%S')}] ${price:,.2f} {r5m_str} {ema_str}{trend_str} imb={ie:+.2f} pos={pos_str}{sl_tag} pnl=${daily_pnl:.2f} cd={cd}s hold={hold}s {lk}")

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
                        _x = "🟢" if pnl_usd >= 0 else "🔴"
                        tg(f"{_x} {pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${price:,.0f} | Hard Stop\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                        local_pos.close("hard_stop"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "hard_stop", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "hard_stop")
                            _x = "🟢" if pnl_usd >= 0 else "🔴"
                            tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | Hard Stop\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                            last_trade_time = now; unlock_entry()
                    do_save_state()
                    await asyncio.sleep(3); continue

                # ── 2. SL CHECK ──
                if local_pos.check_sl(price):
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, local_pos.sl_price, "sl", True)
                        _x = "🟢" if pnl_usd >= 0 else "🔴"
                        tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${local_pos.sl_price:,.0f} | SL\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                        local_pos.close("sl"); last_trade_time = now; unlock_entry()
                    elif local_pos.oco_attached:
                        if ws_is_flat():
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if fill else local_pos.sl_price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "sl")
                            await cancel_stale_orders(signer)
                            local_pos.close("sl"); last_trade_time = now; unlock_entry()
                            _x = "🟢" if pnl_usd >= 0 else "🔴"
                            tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | SL\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                        else:
                            success = await close_position(signer, local_pos, "fast_sl", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "fast_sl")
                                last_trade_time = now; unlock_entry()
                                _x = "🟢" if pnl_usd >= 0 else "🔴"
                                tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | SL\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                    else:
                        success = await close_position(signer, local_pos, "local_sl", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "local_sl")
                            last_trade_time = now; unlock_entry()
                            _x = "🟢" if pnl_usd >= 0 else "🔴"
                            tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | SL\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                    do_save_state()
                    await asyncio.sleep(3); continue

                # ── 3. TRAILING STOP ──
                trail_result = local_pos.update_trailing_stop(price)
                if trail_result == 'trail_exit':
                    best_pct = abs(local_pos.best_price - local_pos.entry_price) / local_pos.entry_price * 100
                    print(f"  TRAIL EXIT! Best: ${local_pos.best_price:,.1f} (+{best_pct:.3f}%)")
                    _s = local_pos.side; _e = local_pos.entry_price; _b = local_pos.best_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "trail_exit", True)
                        _x = "🟢" if pnl_usd >= 0 else "🔴"
                        tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${price:,.0f} | Trail\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                        local_pos.close("trail_exit"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "trail_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "trail_exit")
                            last_trade_time = now; unlock_entry()
                            _x = "🟢" if pnl_usd >= 0 else "🔴"
                            tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | Trail\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                    do_save_state()
                    await asyncio.sleep(1); continue

                # ── 4. TP EXIT ──
                tp_hit = False
                if local_pos.tp_price > 0:
                    if local_pos.side == 'long' and price >= local_pos.tp_price:
                        tp_hit = True
                    elif local_pos.side == 'short' and price <= local_pos.tp_price:
                        tp_hit = True
                if tp_hit:
                    print(f"  TP HIT! Target: ${local_pos.tp_price:,.1f}")
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "tp_exit", True)
                        _x = "🟢" if pnl_usd >= 0 else "🔴"
                        tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${price:,.0f} | TP\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                        local_pos.close("tp_exit"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "tp_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "tp_exit")
                            last_trade_time = now; unlock_entry()
                            _x = "🟢" if pnl_usd >= 0 else "🔴"
                            tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | TP\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                    do_save_state()
                    await asyncio.sleep(1); continue

                # ── 5. RSI PROFIT EXIT ──
                if rsi_refreshed:
                    rsi_exit, rsi_reason = check_rsi_exit(local_pos, price)
                    if rsi_exit:
                        print(f"  RSI EXIT: {rsi_reason} | pnl={unrealized_pct*100:+.3f}%")
                        _s = local_pos.side; _e = local_pos.entry_price
                        if PAPER_TRADE:
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, rsi_reason, True)
                            _x = "🟢" if pnl_usd >= 0 else "🔴"
                            tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${price:,.0f} | RSI Exit\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                            local_pos.close(rsi_reason); last_trade_time = now; unlock_entry()
                        else:
                            success = await close_position(signer, local_pos, rsi_reason, price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, rsi_reason)
                                last_trade_time = now; unlock_entry()
                                _x = "🟢" if pnl_usd >= 0 else "🔴"
                                tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | RSI Exit\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                        do_save_state()
                        await asyncio.sleep(1); continue

                # ── 5. TIME EXIT ──
                effective_max_hold = TRAIL_MAX_HOLD_SECS if local_pos.trailing_active else MAX_HOLD_SECS
                if hold > effective_max_hold:
                    _s = local_pos.side; _e = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, price, "time_exit", True)
                        _x = "🟢" if pnl_usd >= 0 else "🔴"
                        tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${price:,.0f} | Time Exit\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                        local_pos.close("time_exit"); last_trade_time = now; unlock_entry()
                    else:
                        success = await close_position(signer, local_pos, "time_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = handle_exit_pnl(_s, _e, exit_p, "time_exit")
                            last_trade_time = now; unlock_entry()
                            _x = "🟢" if pnl_usd >= 0 else "🔴"
                            tg(f"{_x} ${pnl_usd:+.3f} ({pnl_pct*100:+.2f}%)\n{_s.upper()} ${_e:,.0f} → ${exit_p:,.0f} | Time Exit\nDay: ${daily_pnl:.2f} | {wins}W/{losses}L\n{BOT_VERSION}: ${ver_pnl:+.2f} | {ver_trades}t")
                    do_save_state()
                    await asyncio.sleep(1); continue
                else:
                    sl_tag = "SL" if local_pos.oco_attached else "LOCAL"
                    trail_str = f" trail=${local_pos.trail_stop_price:,.1f}" if local_pos.trailing_active else ""
                    print(f"  Holding {local_pos.side} ${local_pos.entry_price:,.0f} pnl={unrealized_pct*100:.3f}% {hold}s {sl_tag}{trail_str}")

            # ══════════════════════════════════════════════════════════
            # ENTRY LOGIC — RSI zone + turning + VWAP σ + ADX (only check when RSI refreshed)
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
                elif consecutive_losses >= 2 and now - last_loss_time < LOSS_STREAK_COOLDOWN:
                    remaining = int(LOSS_STREAK_COOLDOWN - (now - last_loss_time))
                    print(f"  Loss streak CD ({consecutive_losses}L) {remaining}s")
                elif consecutive_losses > 0 and now - last_loss_time < LOSS_COOLDOWN_SECS:
                    remaining = int(LOSS_COOLDOWN_SECS - (now - last_loss_time))
                    print(f"  Loss CD {remaining}s")
                elif rsi_refreshed:
                    # Evaluate entry when we have fresh RSI data
                    direction, r5m_val, vol_spike = evaluate_entry(price)

                    # Signal debounce — require N consecutive signals in same direction
                    if direction:
                        if signal_debounce["direction"] == direction:
                            signal_debounce["count"] += 1
                            signal_debounce["price"] = price
                        else:
                            signal_debounce = {"direction": direction, "count": 1, "price": price}
                            print(f"  Signal debounce: {direction} 1/{SIGNAL_DEBOUNCE_REQUIRED}")
                    else:
                        if signal_debounce["direction"] is not None:
                            signal_debounce = {"direction": None, "count": 0, "price": 0.0}

                    if direction and signal_debounce["count"] >= SIGNAL_DEBOUNCE_REQUIRED:
                        signal_debounce = {"direction": None, "count": 0, "price": 0.0}  # reset after trigger

                        is_long_entry = (direction == 'long')
                        tag = "LONG" if is_long_entry else "SHORT"
                        vol_str = " +VOL" if vol_spike else ""
                        r5m_disp = f" 5m={r5m_val:.1f}" if r5m_val is not None else ""
                        with rsi_lock:
                            vwap_entry = vwap_current
                        vwap_disp = f" VWAP=${vwap_entry:,.0f}" if vwap_entry is not None else ""

                        trade_entry_context = {"rsi_5m": r5m_val, "vwap": vwap_entry, "vol_spike": vol_spike}

                        if PAPER_TRADE:
                            fill = paper.execute_entry(direction, price)
                            lock_entry(); local_pos.open(direction, fill)
                            _hard_stop_tick_count = 0
                            last_trade_time = now; last_direction = direction; trades_this_hour += 1
                            _emoji = "🟢" if is_long_entry else "🔴"
                            tg(f"{_emoji} {tag} ${fill:,.0f}\nRSI {r5m_val:.1f}\nTP: ${local_pos.tp_price:,.0f}\nSL: ${local_pos.sl_price:,.0f}")
                            do_save_state()
                            await asyncio.sleep(5); continue
                        else:
                            if not ws_is_flat():
                                print(f"  Entry blocked: WS shows position open")
                                lock_entry(); await asyncio.sleep(10); continue

                            lock_entry()
                            print(f"  {tag} ${price:,.0f} RSI{r5m_disp}{vwap_disp} imb={ie:+.2f}{vol_str}")
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
                                    entry_price = price
                                local_pos.open(direction, entry_price)
                                _hard_stop_tick_count = 0
                                last_trade_time = now; last_direction = direction; trades_this_hour += 1
                                _emoji = "🟢" if is_long_entry else "🔴"
                                tg(f"{_emoji} {tag} ${entry_price:,.0f}\nRSI {r5m_val:.1f}\nTP: ${local_pos.tp_price:,.0f}\nSL: ${local_pos.sl_price:,.0f}")
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
