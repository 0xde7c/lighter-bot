#!/usr/bin/env python3
"""
Momentum Bot v7.0 — Trend Following Strategy
  Replaces v6.5.7 mean-reversion scalper with momentum/trend following.
  Key changes:
  1. Multi-timeframe velocity alignment (60s/180s/300s)
  2. Volume-weighted tape momentum confirmation
  3. Directional efficiency (chop) filter
  4. Wider stops (0.10% SL, 0.14% hard stop)
  5. Longer holds (5min normal, 7min trailing)
  6. Momentum exhaustion exit
  7. Loss streak cooldown tracking
"""
import asyncio, time, requests, lighter, threading, json, collections, os, traceback
from datetime import datetime
import websocket as ws_lib
from lighter.signer_client import CreateOrderTxReq  # needed for OCO orders

# ══════════════════════════════════════════════════════════════════════════
# MODE
# ══════════════════════════════════════════════════════════════════════════
PAPER_TRADE = True
PAPER_STARTING_BALANCE = 71.30

# ── CONFIG ───────────────────────────────────────────────────────────────
ACCOUNT_INDEX=716892; API_KEY_INDEX=3
API_PRIVATE_KEY="5a9634234bac159b30afd8f6bca2a4fa57a46e6318a37447efd364ee90756d2138da41588bdabe55"
LIGHTER_URL="https://mainnet.zklighter.elliot.ai"; MARKET_INDEX=1
TG_TOKEN="8536956116:AAGOWByFex_n1wraFuGFwf7e5YPVe2Vyegw"; TG_CHAT="5583279698"

BASE_AMOUNT=500  # 0.005 BTC (~$335 notional)

# ── ENTRY ────────────────────────────────────────────────────────────────
VELOCITY_60_MIN = 0.03      # 0.03% min 1-min velocity
VELOCITY_180_MIN = 0.05     # 0.05% min 3-min velocity
VELOCITY_300_MIN = 0.06     # 0.06% min 5-min velocity
TAPE_30_MIN = 0.15          # min 30s tape momentum
TAPE_120_MIN = 0.10         # min 120s tape momentum
IMB_CONFIRM_THRESHOLD = 0.15 # OB imbalance confirmation (lowered from 0.38)
ENTRY_MIN_SCORE = 40        # minimum quality score (0-100)

# ── EXITS ────────────────────────────────────────────────────────────────
SL_PCT = 0.0010             # 0.10% SL on exchange (was 0.04%)
HARD_STOP_PCT = 0.0014      # 0.14% hard stop (was 0.06%)
TRAIL_ACTIVATE_PCT = 0.0008 # 0.08% to activate trail (was 0.04%)
TRAIL_DISTANCE_PCT = 0.0005 # 0.05% trail distance (was 0.03%)
MAX_HOLD_SECS = 300         # 5 min normal (was 60s)
TRAIL_MAX_HOLD_SECS = 420   # 7 min trailing (was 90s)
LOCK_DURATION = 450          # covers max hold + settle

# ── FILTERS ──────────────────────────────────────────────────────────────
VOL_LOOKBACK_SECS = 300
VOL_MIN_RANGE_PCT = 0.04    # min 5min range (was 0.10)
VOL_MAX_RANGE_PCT = 0.60    # max 5min range (NEW — avoid chaos)
CHOP_MIN_EFFICIENCY = 0.35  # directional efficiency ratio (replaces range filter)
OVEREXT_MAX_PCT = 0.30      # block entries after 0.30% move in 5min
SPREAD_MAX_BPS = 5.0        # max bid-ask spread (NEW)

# ── COOLDOWNS ────────────────────────────────────────────────────────────
MIN_TRADE_INTERVAL = 30     # 30s between trades (was 20s)
LOSS_COOLDOWN_SECS = 60     # extra cooldown after loss
LOSS_STREAK_COOLDOWN = 120  # cooldown after 3 consecutive losses
WARMUP_SECS = 330           # 5.5 min warmup on startup

MAX_TRADES_HOUR = 60
DAILY_LOSS_LIMIT = 2.0

# ── SIGNALS (kept for compatibility) ─────────────────────────────────────
IMB_LEVELS = 5; IMB_EMA_ALPHA = 0.1
IMB_EXIT_THRESHOLD = 0.15
IMB_EXIT_MIN_PROFIT = 0.0002  # 0.02% min profit for imb exit
SMOOTH_TICKS = 10; LOOKBACK_TICKS = 30

TP_PCT = 0.0010             # 0.10% TP — only used for trailing stop reference

# ── SIMPLIFIED EXIT ─────────────────────────────────────────────────────
EXIT_SETTLE_SECS = 5

STATE_FILE="/root/rsi_bot/.bot_state_v7paper.json"
LOCK_FILE="/root/rsi_bot/.entry_lock_v7paper"
LOCAL_POS_FILE="/root/rsi_bot/.local_position_v7paper.json"

tick_prices=collections.deque(maxlen=1200); tick_lock=threading.Lock()
ob_bids=[]; ob_asks=[]; ob_lock=threading.Lock()
imb_ema=0.0; imb_ema_lock=threading.Lock()
momentum_history=collections.deque(maxlen=1200)

# v6.5: Account websocket state
acct_ws_lock = threading.Lock()
acct_ws_position = {"size": 0.0, "sign": 0, "entry": 0.0, "open_orders": 0}
acct_ws_balance = 0.0
acct_ws_last_trade = None
acct_ws_ready = threading.Event()
acct_ws_trade_queue = collections.deque(maxlen=50)

# v6.5.1: Trade tape state — increased maxlen for momentum calculations
tape_lock = threading.Lock()
tape_recent = collections.deque(maxlen=500)  # increased from 200 for tape momentum
tape_whale_threshold = 0.5
tape_last_whale = None

# v7.0: Bot start time for warmup
bot_start_time = time.time()


# ══════════════════════════════════════════════════════════════════════════
# API — only used for balance check and startup orphan detection
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
        sign = 1 if float(pos.get("position", 0)) > 0 else -1
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
        if side == 'long':
            self.tp_price = entry_price * (1 + TP_PCT)
            self.sl_price = entry_price * (1 - SL_PCT)
        else:
            self.tp_price = entry_price * (1 - TP_PCT)
            self.sl_price = entry_price * (1 + SL_PCT)
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

    def check_tp_sl(self, current_price):
        if not self.in_position: return None
        if self.side == 'long':
            if current_price >= self.tp_price: return 'tp'
            if current_price <= self.sl_price: return 'sl'
        else:
            if current_price <= self.tp_price: return 'tp'
            if current_price >= self.sl_price: return 'sl'
        return None

    def update_trailing_stop(self, current_price):
        """Update trailing stop. Returns 'trail_exit' if triggered, None otherwise."""
        if not self.in_position: return None
        profit_pct = self.unrealized_pct(current_price)

        if not self.trailing_active and profit_pct >= TRAIL_ACTIVATE_PCT:
            self.trailing_active = True
            self.best_price = current_price
            if self.side == 'long':
                self.trail_stop_price = current_price * (1 - TRAIL_DISTANCE_PCT)
            else:
                self.trail_stop_price = current_price * (1 + TRAIL_DISTANCE_PCT)
            print(f"  TRAIL ACTIVATED at +{profit_pct*100:.3f}% | stop=${self.trail_stop_price:,.1f}")
            return None

        if not self.trailing_active: return None

        if self.side == 'long':
            if current_price > self.best_price:
                self.best_price = current_price
                self.trail_stop_price = current_price * (1 - TRAIL_DISTANCE_PCT)
            if current_price <= self.trail_stop_price:
                return 'trail_exit'
        else:
            if current_price < self.best_price:
                self.best_price = current_price
                self.trail_stop_price = current_price * (1 + TRAIL_DISTANCE_PCT)
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


# ── WS FEED ─────────────────────────────────────────────────────────────
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

            # ── ORDERBOOK UPDATE ──
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

            # ── ACCOUNT UPDATE ──
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

            # ── TRADE TAPE — market-wide fills ──
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
                            "price": price,
                            "size": size,
                            "side": side,
                            "time": time.time(),
                            "usd": float(t.get("usd_amount", "0")),
                            "ask_account": t.get("ask_account_id", 0),
                            "bid_account": t.get("bid_account_id", 0),
                        }
                        tape_recent.append(trade_info)

                        if size >= tape_whale_threshold:
                            tape_last_whale = trade_info
                            print(f"  WHALE {side.upper()} {size:.4f} BTC (${trade_info['usd']:,.0f}) @ ${price:,.1f}")

                        if (t.get("ask_account_id") == ACCOUNT_INDEX or
                            t.get("bid_account_id") == ACCOUNT_INDEX):
                            with acct_ws_lock:
                                acct_ws_last_trade = {
                                    "price": price,
                                    "size": size,
                                    "side": side,
                                    "trade_id": t.get("trade_id", 0),
                                    "time": time.time(),
                                    "is_our_trade": True
                                }
                                acct_ws_trade_queue.append(acct_ws_last_trade)
                                print(f"  OUR FILL: {side} {size:.5f} @ ${price:,.1f}")

        except Exception as e:
            pass  # Don't crash WS thread on parse errors

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
# SIGNALS — kept from v6.5.7
# ══════════════════════════════════════════════════════════════════════════
def get_imb_ema():
    with imb_ema_lock: return imb_ema

def get_momentum():
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < LOOKBACK_TICKS + SMOOTH_TICKS: return None, None
    current = sum(t[1] for t in ticks[-SMOOTH_TICKS:]) / SMOOTH_TICKS
    pe = len(ticks) - LOOKBACK_TICKS; ps = max(0, pe - SMOOTH_TICKS)
    past = sum(t[1] for t in ticks[ps:pe]) / (pe - ps)
    mom = (current - past) / past; momentum_history.append(mom)
    return current, mom


# ══════════════════════════════════════════════════════════════════════════
# v7.0: NEW SIGNAL FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════

def is_warmed_up():
    """Check if bot has been running long enough to have reliable data."""
    return time.time() - bot_start_time >= WARMUP_SECS

def compute_velocity(window_secs):
    """Price velocity over N seconds. Returns percentage change or None."""
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < 10: return None
    now = time.time()
    recent = [t for t in ticks if now - t[0] <= window_secs]
    if len(recent) < 5: return None
    old_price = recent[0][1]
    new_price = recent[-1][1]
    if old_price == 0: return None
    return ((new_price - old_price) / old_price) * 100  # return as percentage

def compute_tape_momentum(window_secs):
    """Volume-weighted buy/sell pressure with time decay. Returns -1.0 to +1.0."""
    with tape_lock: trades = list(tape_recent)
    if not trades: return 0.0
    now = time.time()
    recent = [t for t in trades if now - t["time"] <= window_secs]
    if len(recent) < 3: return 0.0

    buy_pressure = 0.0
    sell_pressure = 0.0
    for t in recent:
        age = now - t["time"]
        # Linear decay: weight = 1.0 at t=0, approaches 0 at t=window_secs
        weight = max(0.0, 1.0 - (age / window_secs))
        vol = t["size"] * weight
        if t["side"] == "buy":
            buy_pressure += vol
        else:
            sell_pressure += vol

    total = buy_pressure + sell_pressure
    if total == 0: return 0.0
    return (buy_pressure - sell_pressure) / total

def evaluate_entry():
    """
    Three-layer entry signal for momentum strategy.
    Returns (direction, quality_score) or (None, 0).
    """
    # Layer 1: Multi-timeframe velocity alignment
    v60 = compute_velocity(60)
    v180 = compute_velocity(180)
    v300 = compute_velocity(300)

    if v60 is None or v180 is None or v300 is None:
        return None, 0

    # All three must have same sign and exceed minimums
    long_vel = (v60 > VELOCITY_60_MIN and v180 > VELOCITY_180_MIN and v300 > VELOCITY_300_MIN)
    short_vel = (v60 < -VELOCITY_60_MIN and v180 < -VELOCITY_180_MIN and v300 < -VELOCITY_300_MIN)

    if not long_vel and not short_vel:
        return None, 0

    direction = 'long' if long_vel else 'short'

    # Layer 2: Tape momentum confirmation
    tape_30 = compute_tape_momentum(30)
    tape_120 = compute_tape_momentum(120)

    if direction == 'long':
        if tape_30 < TAPE_30_MIN or tape_120 < TAPE_120_MIN:
            return None, 0
    else:
        if tape_30 > -TAPE_30_MIN or tape_120 > -TAPE_120_MIN:
            return None, 0

    # Layer 3: Order book imbalance confirmation
    ie = get_imb_ema()
    if direction == 'long' and ie < IMB_CONFIRM_THRESHOLD:
        return None, 0
    if direction == 'short' and ie > -IMB_CONFIRM_THRESHOLD:
        return None, 0

    # Calculate quality score (0-100)
    # Velocity strength: how much above minimum (max 40 pts)
    if direction == 'long':
        vel_score = min(40, (
            (v60 / VELOCITY_60_MIN - 1) * 10 +
            (v180 / VELOCITY_180_MIN - 1) * 10 +
            (v300 / VELOCITY_300_MIN - 1) * 10
        ))
        # Tape strength (max 30 pts)
        tape_score = min(30, (abs(tape_30) / TAPE_30_MIN - 1) * 15 + (abs(tape_120) / TAPE_120_MIN - 1) * 15)
        # OB confirmation (max 30 pts)
        ob_score = min(30, (ie / IMB_CONFIRM_THRESHOLD - 1) * 15)
    else:
        vel_score = min(40, (
            (abs(v60) / VELOCITY_60_MIN - 1) * 10 +
            (abs(v180) / VELOCITY_180_MIN - 1) * 10 +
            (abs(v300) / VELOCITY_300_MIN - 1) * 10
        ))
        tape_score = min(30, (abs(tape_30) / TAPE_30_MIN - 1) * 15 + (abs(tape_120) / TAPE_120_MIN - 1) * 15)
        ob_score = min(30, (abs(ie) / IMB_CONFIRM_THRESHOLD - 1) * 15)

    score = max(0, int(vel_score + tape_score + ob_score))

    if score < ENTRY_MIN_SCORE:
        return None, 0

    print(f"  SIGNAL: {direction.upper()} score={score} v60={v60:+.3f}% v180={v180:+.3f}% v300={v300:+.3f}%")
    print(f"    tape30={tape_30:+.2f} tape120={tape_120:+.2f} imb={ie:+.3f}")

    return direction, score

def volatility_ok():
    """Check 5min range is within acceptable bounds."""
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < 30: return True
    now = time.time()
    recent = [t[1] for t in ticks if now - t[0] <= VOL_LOOKBACK_SECS]
    if len(recent) < 10: return True
    high = max(recent); low = min(recent)
    range_pct = (high - low) / low * 100
    if range_pct < VOL_MIN_RANGE_PCT:
        print(f"  Low vol: {range_pct:.3f}% (need {VOL_MIN_RANGE_PCT}%)")
        return False
    if range_pct > VOL_MAX_RANGE_PCT:
        print(f"  Chaos vol: {range_pct:.3f}% (max {VOL_MAX_RANGE_PCT}%)")
        return False
    return True

def chop_filter():
    """v7.0: Directional efficiency ratio over 5 minutes. Replaces old range-based chop filter."""
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < 60: return True  # not enough data
    now = time.time()
    recent = [t for t in ticks if now - t[0] <= 300]  # 5 minutes
    if len(recent) < 30: return True

    # Calculate directional efficiency: |net_move| / sum(|each_tick_move|)
    net_move = abs(recent[-1][1] - recent[0][1])
    total_move = 0.0
    for i in range(1, len(recent)):
        total_move += abs(recent[i][1] - recent[i-1][1])

    if total_move == 0: return False
    efficiency = net_move / total_move

    if efficiency < CHOP_MIN_EFFICIENCY:
        print(f"  Chop: efficiency={efficiency:.2f} (need {CHOP_MIN_EFFICIENCY})")
        return False
    return True

def overextension_filter():
    """v7.0: Block entries when 5min move > 0.30% (already extended)."""
    v300 = compute_velocity(300)
    if v300 is None: return True
    if abs(v300) > OVEREXT_MAX_PCT:
        print(f"  Overextended: {v300:+.3f}% 5min (max {OVEREXT_MAX_PCT}%)")
        return False
    return True

def spread_filter():
    """v7.0: Block entries when bid-ask spread is too wide."""
    with ob_lock:
        bids = list(ob_bids[:1])
        asks = list(ob_asks[:1])
    if not bids or not asks: return True
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_bid == 0: return True
    spread_bps = ((best_ask - best_bid) / best_bid) * 10000
    if spread_bps > SPREAD_MAX_BPS:
        print(f"  Wide spread: {spread_bps:.1f}bps (max {SPREAD_MAX_BPS})")
        return False
    return True

def check_momentum_exhaustion(local_pos, price):
    """
    v7.0: Early exit when momentum flips against position.
    Returns True if should exit.
    Conditions: in profit (>0.04%) AND (tape momentum flips OR 60s velocity reverses).
    """
    if not local_pos.in_position: return False
    profit_pct = local_pos.unrealized_pct(price)
    if profit_pct < 0.0004: return False  # need at least 0.04% profit

    # Check tape momentum flip
    tape_30 = compute_tape_momentum(30)
    # Check 60s velocity reversal
    v60 = compute_velocity(60)

    if local_pos.side == 'long':
        tape_flipped = tape_30 < -0.10
        vel_reversed = v60 is not None and v60 < -0.02
    else:
        tape_flipped = tape_30 > 0.10
        vel_reversed = v60 is not None and v60 > 0.02

    if tape_flipped or vel_reversed:
        reason = "tape_flip" if tape_flipped else "vel_reverse"
        print(f"  MOMENTUM EXHAUSTION: {reason} | profit={profit_pct*100:.3f}% tape30={tape_30:+.2f} v60={v60:+.3f}%" if v60 else f"  MOMENTUM EXHAUSTION: {reason} | profit={profit_pct*100:.3f}% tape30={tape_30:+.2f}")
        return True
    return False


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
    with acct_ws_lock:
        return dict(acct_ws_position)

def ws_is_flat():
    with acct_ws_lock:
        return acct_ws_position["size"] == 0.0

def ws_get_last_fill():
    with acct_ws_lock:
        return dict(acct_ws_last_trade) if acct_ws_last_trade else None

def ws_get_balance():
    with acct_ws_lock:
        return acct_ws_balance

async def ws_wait_for_flat(timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        if ws_is_flat():
            return True
        await asyncio.sleep(0.2)
    return False

async def ws_wait_for_position(timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        pos = ws_get_position()
        if pos["size"] > 0:
            return True
        await asyncio.sleep(0.2)
    return False


async def position_sync_check(signer, local_pos):
    """Detect and fix position mismatches every loop."""
    ws_pos = ws_get_position()

    if not local_pos.in_position and ws_pos["size"] > 0:
        is_long = ws_pos["sign"] > 0
        side_str = "LONG" if is_long else "SHORT"
        print(f"  SYNC: Naked {side_str} detected! Size={ws_pos['size']:.5f} @ ${ws_pos['entry']:,.1f}")
        tg(f"SYNC: Naked {side_str} detected!\nSize: {ws_pos['size']:.5f} @ ${ws_pos['entry']:,.1f}\nAuto-closing...")
        try:
            await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
            await asyncio.sleep(1)
        except: pass
        try:
            _, _, err = await signer.create_market_order_limited_slippage(
                market_index=MARKET_INDEX, client_order_index=0,
                base_amount=int(ws_pos["size"] * 100000),
                max_slippage=0.01,
                is_ask=1 if is_long else 0,
                reduce_only=True
            )
            if not err:
                await asyncio.sleep(3)
                if ws_is_flat():
                    print(f"  SYNC: Naked position closed")
                    tg(f"Naked position auto-closed")
                else:
                    print(f"  SYNC: Close sent but still open")
                    tg(f"Naked close may have failed -- check manually!")
            else:
                print(f"  SYNC: Close failed: {err}")
                tg(f"Naked close failed: {err}\nClose manually!")
        except Exception as e:
            print(f"  SYNC: Exception: {e}")
            tg(f"Naked close error: {e}\nClose manually!")
        return True

    if local_pos.in_position and ws_pos["size"] == 0:
        rest_pos = api_get_position()
        if rest_pos and rest_pos["size"] == 0:
            print(f"  SYNC: Bot thinks {local_pos.side} but exchange flat -- syncing")
            tg(f"SYNC: Position gone\nWas: {local_pos.side} @ ${local_pos.entry_price:,.0f}")
            local_pos.close("sync_flat")
            return True

    return False


# v6.5.1: Trade tape helpers (kept)
def tape_get_recent_volume(seconds=5):
    with tape_lock:
        cutoff = time.time() - seconds
        return sum(t["size"] for t in tape_recent if t["time"] > cutoff)

def tape_get_recent_bias(seconds=5):
    with tape_lock:
        cutoff = time.time() - seconds
        recent = [t for t in tape_recent if t["time"] > cutoff]
        if not recent:
            return 0.0
        buy_vol = sum(t["size"] for t in recent if t["side"] == "buy")
        sell_vol = sum(t["size"] for t in recent if t["side"] == "sell")
        total = buy_vol + sell_vol
        return (buy_vol - sell_vol) / total if total > 0 else 0.0


# ── STATE & TG ──────────────────────────────────────────────────────────
def save_state(data):
    try:
        with open(STATE_FILE, "w") as f: json.dump(data, f)
    except: pass

def load_state():
    d = {"last_trade_time": 0, "last_direction": None,
         "daily_pnl": 0, "wins": 0, "losses": 0, "daily_trades": 0, "last_day": time.strftime("%d"),
         "consecutive_losses": 0, "last_loss_time": 0}
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
    """Attach SL order only — trailing stop handles the upside."""
    try:
        if is_long:
            sl_p = entry * (1 - SL_PCT); ea = 1
            sl_l = int(sl_p * 0.999 * 10)
        else:
            sl_p = entry * (1 + SL_PCT); ea = 0
            sl_l = int(sl_p * 1.001 * 10)
        sl_t = int(sl_p * 10)
        _, _, err = await signer.create_sl_order(
            market_index=MARKET_INDEX,
            client_order_index=0,
            base_amount=BASE_AMOUNT,
            price=sl_l,
            is_ask=ea,
            trigger_price=sl_t
        )
        return err, sl_p
    except Exception as e:
        try:
            sl_o = CreateOrderTxReq(MarketIndex=MARKET_INDEX, ClientOrderIndex=0, BaseAmount=BASE_AMOUNT, Price=sl_l, IsAsk=ea,
                Type=signer.ORDER_TYPE_STOP_LOSS_LIMIT, TimeInForce=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                ReduceOnly=1, TriggerPrice=sl_t, OrderExpiry=-1)
            _, _, err = await signer.create_grouped_orders(grouping_type=signer.GROUPING_TYPE_ONE_CANCELS_THE_OTHER, orders=[sl_o])
            return err, sl_p
        except Exception as e2:
            return str(e2), 0

async def attach_oco_background(signer, is_long, entry_price):
    """Attach SL with staleness check — skip if position changed."""
    expected_entry_time = local_pos.entry_time
    await asyncio.sleep(2)
    if local_pos.entry_time != expected_entry_time:
        print(f"  SL skipped -- position changed (stale task)")
        return
    for attempt in range(3):
        if local_pos.entry_time != expected_entry_time:
            print(f"  SL skipped -- position changed (stale task)")
            return
        try:
            err, sl_p = await attach_sl_order(signer, entry_price, is_long)
            if not err:
                local_pos.oco_attached = True; local_pos._save()
                tg(f"SL:${sl_p:,.0f} (trailing stop manages TP)")
                return
            print(f"  SL attempt {attempt+1}: {err}")
        except Exception as e:
            print(f"  SL attempt {attempt+1}: {e}")
        await asyncio.sleep(2)
    if local_pos.entry_time == expected_entry_time:
        local_pos.oco_attached = False; local_pos._save()
        tg("SL order failed -- bot monitoring SL locally")


# ══════════════════════════════════════════════════════════════════════════
# SIMPLIFIED EXIT
# ══════════════════════════════════════════════════════════════════════════
async def send_market_close(signer, is_long):
    try:
        await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
        await asyncio.sleep(1)
    except Exception as e:
        print(f"  Cancel error: {e}")

    try:
        _, _, err = await signer.create_market_order_limited_slippage(
            market_index=MARKET_INDEX,
            client_order_index=0,
            base_amount=BASE_AMOUNT,
            max_slippage=0.01,
            is_ask=1 if is_long else 0,
            reduce_only=True
        )
    except Exception as e:
        print(f"  ReduceOnly failed ({e}), checking WS...")
        if ws_is_flat():
            print(f"  WS shows flat -- position already closed")
            return True
        print(f"  ReduceOnly failed and position still open!")
        tg(f"ReduceOnly close failed!\nPosition still open -- close manually!\n{e}")
        err = str(e)

    if err:
        print(f"  Exit order failed: {err}")
        tg(f"Exit failed: {err}")
        return False
    return True

async def cancel_stale_orders(signer):
    try:
        await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
        print(f"  Cancelled stale orders")
    except Exception as e:
        print(f"  Stale order cancel failed: {e}")

async def close_position(signer, local_pos, reason, price):
    is_long = (local_pos.side == 'long')
    entry = local_pos.entry_price

    if PAPER_TRADE:
        return True

    print(f"  Closing: {reason}")
    success = await send_market_close(signer, is_long)

    if success:
        print(f"  Waiting for WS position confirmation...")
        flat = await ws_wait_for_flat(timeout=8)
        if flat:
            print(f"  WS confirmed: position flat")
            await cancel_stale_orders(signer)
            local_pos.close(reason)
            return True
        else:
            print(f"  WS timeout -- checking REST API...")
            pos = api_get_position()
            if pos and pos["size"] == 0:
                print(f"  REST confirms: position flat")
                await cancel_stale_orders(signer)
                local_pos.close(reason)
                return True
            elif pos and pos["size"] > 0:
                print(f"  REST shows position still open! Waiting 3s then re-checking...")
                await asyncio.sleep(3)
                if ws_is_flat():
                    print(f"  WS now shows flat -- first close filled late")
                    local_pos.close(reason)
                    return True
                pos2 = api_get_position()
                if pos2 and pos2["size"] == 0:
                    print(f"  REST now confirms flat")
                    local_pos.close(reason)
                    return True
                print(f"  Still open after 3s -- retrying close with ReduceOnly...")
                success2 = await send_market_close(signer, is_long)
                if success2:
                    flat2 = await ws_wait_for_flat(timeout=8)
                    if flat2:
                        print(f"  WS confirmed flat on retry")
                    else:
                        print(f"  Still not confirmed -- check manually")
                        tg(f"Exit uncertain -- check position!")
                local_pos.close(reason)
                return True
            else:
                print(f"  REST API failed -- assuming closed")
                local_pos.close(reason)
                return True
    else:
        print(f"  Retry exit...")
        await asyncio.sleep(2)
        success2 = await send_market_close(signer, is_long)
        if success2:
            flat = await ws_wait_for_flat(timeout=8)
            if flat:
                print(f"  WS confirmed: position flat")
            else:
                pos = api_get_position()
                if pos and pos["size"] > 0:
                    print(f"  REST shows still open after retry!")
                    tg(f"Exit may have failed -- check position!")
                else:
                    print(f"  REST confirms flat on retry")
            local_pos.close(reason)
            return True
        else:
            tg(f"EXIT FAILED TWICE! Close manually!\n{local_pos.side} @ ${entry:,.0f}")
            return False


# ══════════════════════════════════════════════════════════════════════════
# STARTUP POSITION CLEANUP
# ══════════════════════════════════════════════════════════════════════════
async def startup_close_orphan(signer):
    print("  Checking for orphaned positions...")

    for attempt in range(3):
        pos = api_get_position()
        if pos is None:
            print(f"  API failed (attempt {attempt+1}/3), retrying...")
            await asyncio.sleep(2)
            continue

        if pos["size"] == 0 and pos["open_orders"] == 0:
            print(f"  No orphaned position. Balance: ${pos['balance']:.2f}")
            return pos["balance"]

        if pos["open_orders"] > 0:
            print(f"  Cancelling {pos['open_orders']} orphaned orders...")
            try:
                await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"  Cancel error: {e}")

        if pos["size"] > 0:
            is_long = (pos["sign"] > 0)
            side_str = "LONG" if is_long else "SHORT"
            print(f"  Orphaned {side_str} position: {pos['size']:.5f} BTC @ ${pos['entry']:,.1f}")
            print(f"  Auto-closing orphaned position...")
            tg(f"Orphaned {side_str} found on startup\nSize: {pos['size']:.5f} @ ${pos['entry']:,.1f}\nAuto-closing...")

            try:
                _, _, err = await signer.create_market_order_limited_slippage(
                    market_index=MARKET_INDEX,
                    client_order_index=0,
                    base_amount=int(pos["size"] * 100000),
                    max_slippage=0.01,
                    is_ask=1 if is_long else 0,
                    reduce_only=True
                )
            except Exception as e:
                print(f"  ReduceOnly orphan close failed ({e})")
                tg(f"Orphan close failed!\n{e}\nClose manually on Lighter!")
                err = str(e)

            if err:
                print(f"  Close failed: {err}")
                tg(f"Failed to close orphan: {err}\nClose manually!")
                await asyncio.sleep(3)
                continue

            print(f"  Waiting for settlement...")
            await asyncio.sleep(EXIT_SETTLE_SECS + 2)

            pos2 = api_get_position()
            if pos2 and pos2["size"] == 0:
                print(f"  Orphaned position closed. Balance: ${pos2['balance']:.2f}")
                tg(f"Orphaned position closed\nBalance: ${pos2['balance']:.2f}")
                return pos2["balance"]
            else:
                print(f"  Position may still be open, retrying...")
                await asyncio.sleep(2)
                continue

    print(f"  Could not clear orphaned position after 3 attempts!")
    tg(f"STARTUP FAILED: Could not close orphaned position!\nClose manually and restart.")
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

def track_direction_pnl(side, pnl):
    global long_pnl, short_pnl, long_trades, short_trades
    if side == 'long':
        long_pnl += pnl; long_trades += 1
    else:
        short_pnl += pnl; short_trades += 1

last_direction = state["last_direction"]

if last_day != time.strftime("%d"):
    last_day = time.strftime("%d")

local_pos = LocalPosition()
paper = PaperTrader(PAPER_STARTING_BALANCE) if PAPER_TRADE else None

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
                price, mom = get_momentum()
                ie = get_imb_ema()
                v60 = compute_velocity(60)
                v180 = compute_velocity(180)
                v300 = compute_velocity(300)
                tape_30 = compute_tape_momentum(30)
                tape_120 = compute_tape_momentum(120)

                # Directional efficiency
                with tick_lock: ticks = list(tick_prices)
                eff = 0.0
                if len(ticks) >= 60:
                    now = time.time()
                    recent = [t for t in ticks if now - t[0] <= 300]
                    if len(recent) >= 30:
                        net = abs(recent[-1][1] - recent[0][1])
                        total_m = sum(abs(recent[i][1] - recent[i-1][1]) for i in range(1, len(recent)))
                        eff = net / total_m if total_m > 0 else 0

                pos_str = f"{local_pos.side} @ ${local_pos.entry_price:,.0f}" if local_pos.in_position else "flat"
                oco_str = " SL:ON" if local_pos.oco_attached else " SL:LOCAL" if local_pos.in_position else ""
                mode = "PAPER" if PAPER_TRADE else "LIVE"
                wr = f"{wins/(wins+losses)*100:.0f}%" if wins+losses > 0 else "N/A"

                vel_str = "n/a"
                if v60 is not None and v180 is not None and v300 is not None:
                    vel_str = f"{v60:+.3f}/{v180:+.3f}/{v300:+.3f}%"

                warmup_left = max(0, int(WARMUP_SECS - (time.time() - bot_start_time)))
                warmup_str = f"\nWarmup: {warmup_left}s" if warmup_left > 0 else ""

                tg(f"v7.0 [{mode}]\n${price:,.0f} | Imb:{ie:+.2f}\n"
                   f"Vel(60/180/300): {vel_str}\n"
                   f"Tape(30/120): {tape_30:+.2f}/{tape_120:+.2f}\n"
                   f"Efficiency: {eff:.2f} | Streak: {consecutive_losses}L\n"
                   f"Pos: {pos_str}{oco_str}\n"
                   f"Day: ${daily_pnl:.2f} | {wins}W/{losses}L ({wr})\n"
                   f"L: {long_trades}t ${long_pnl:+.2f} | S: {short_trades}t ${short_pnl:+.2f}\n"
                   f"SL:0.10% HS:0.14% Hold:{MAX_HOLD_SECS}s Trail:{TRAIL_MAX_HOLD_SECS}s"
                   f"{warmup_str}")
            elif msg == "/trades" and PAPER_TRADE and paper:
                if not paper.trade_log: tg("No trades yet")
                else:
                    last5 = paper.trade_log[-5:]
                    lines = [f"{t['time']} {t['side']} {t['reason']} ${t['pnl_usd']:+.2f}" for t in last5]
                    tg("Last 5:\n" + "\n".join(lines) + f"\n\n{paper.summary()}")
            elif msg == "/help": tg("/pause /resume /status /trades /help")
    except: pass


# ══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════
async def main():
    global daily_pnl, daily_trades, trades_this_hour, hour_reset_time, last_trade_time
    global wins, losses, last_day, paused, last_direction
    global consecutive_losses, last_loss_time, bot_start_time

    mode_str = "PAPER" if PAPER_TRADE else "LIVE"
    print(f"Momentum Bot v7.0 [{mode_str}]")
    print(f"  SL:{SL_PCT*100:.2f}% HardStop:{HARD_STOP_PCT*100:.2f}%")
    print(f"  Trail: activate@{TRAIL_ACTIVATE_PCT*100:.2f}% distance:{TRAIL_DISTANCE_PCT*100:.3f}% max_hold:{TRAIL_MAX_HOLD_SECS}s")
    print(f"  Hold:{MAX_HOLD_SECS}s CD:{MIN_TRADE_INTERVAL}s Warmup:{WARMUP_SECS}s")
    print(f"  Velocity min: 60s={VELOCITY_60_MIN}% 180s={VELOCITY_180_MIN}% 300s={VELOCITY_300_MIN}%")
    print(f"  Tape min: 30s={TAPE_30_MIN} 120s={TAPE_120_MIN} | Imb confirm: {IMB_CONFIRM_THRESHOLD}")
    print(f"  Vol: {VOL_MIN_RANGE_PCT}-{VOL_MAX_RANGE_PCT}% | Chop eff: {CHOP_MIN_EFFICIENCY} | Overext: {OVEREXT_MAX_PCT}%")
    print(f"  Spread max: {SPREAD_MAX_BPS}bps | Loss streak CD: {LOSS_STREAK_COOLDOWN}s")

    bot_start_time = time.time()

    threading.Thread(target=ws_thread, daemon=True).start()
    await asyncio.sleep(3)
    if not PAPER_TRADE:
        print("  Waiting for account WS snapshot...")
        acct_ws_ready.wait(timeout=10)
        if acct_ws_ready.is_set():
            print(f"  Account WS ready. Balance: ${ws_get_balance():.2f}")
        else:
            print(f"  Account WS not ready -- continuing with REST fallback")

    signer = None
    if not PAPER_TRADE:
        signer = lighter.SignerClient(url=LIGHTER_URL, account_index=ACCOUNT_INDEX,
            api_private_keys={API_KEY_INDEX: API_PRIVATE_KEY})
        signer.create_client(api_key_index=API_KEY_INDEX)

    # STARTUP SAFETY — clear any orphaned positions
    if local_pos.in_position:
        print(f"  Stale local position -- clearing file")
        local_pos.close("stale_startup")
    unlock_entry()

    starting_bal = None
    if not PAPER_TRADE:
        starting_balance = await startup_close_orphan(signer)
        if starting_balance is None:
            print("  ABORTING: Could not ensure clean state on startup")
            tg("Bot aborted -- could not close orphaned position. Fix manually.")
            return
        starting_bal = starting_balance

    tg(f"v7.0 MOMENTUM [{mode_str}]\n"
       f"SL:{SL_PCT*100:.2f}% HS:{HARD_STOP_PCT*100:.2f}%\n"
       f"Trail: @{TRAIL_ACTIVATE_PCT*100:.2f}% dist:{TRAIL_DISTANCE_PCT*100:.3f}%\n"
       f"Hold:{MAX_HOLD_SECS}s (trail:{TRAIL_MAX_HOLD_SECS}s)\n"
       f"Vel: {VELOCITY_60_MIN}/{VELOCITY_180_MIN}/{VELOCITY_300_MIN}%\n"
       f"Warmup: {WARMUP_SECS}s"
       + (f"\nPaper: ${paper.balance:.2f}" if PAPER_TRADE else f"\nBalance: ${starting_bal:.2f}"))

    last_tg_check = 0

    while True:
        try:
            now = time.time()
            if now - hour_reset_time > 3600: trades_this_hour = 0; hour_reset_time = now
            today = time.strftime("%d")
            if today != last_day:
                daily_pnl = 0.0; daily_trades = 0; wins = 0; losses = 0; last_day = today
                consecutive_losses = 0
                if PAPER_TRADE and paper: paper.daily_pnl = 0.0
                tg("Daily reset")
                save_state({"last_trade_time": last_trade_time,
                    "last_direction": last_direction, "daily_pnl": 0, "wins": 0, "losses": 0,
                    "daily_trades": 0, "last_day": today, "consecutive_losses": 0, "last_loss_time": 0})
            if now - last_tg_check > 3: tg_check(); last_tg_check = now

            price_mom = get_momentum()
            if price_mom[0] is None: await asyncio.sleep(1); continue
            price, momentum = price_mom
            ie = get_imb_ema()

            # Position sync
            if not PAPER_TRADE and signer:
                sync_handled = await position_sync_check(signer, local_pos)
                if sync_handled:
                    await asyncio.sleep(3); continue
            locked = is_entry_locked()
            cd = max(0, int(last_trade_time + MIN_TRADE_INTERVAL - now))
            hold = local_pos.hold_time()
            with tick_lock: n_ticks = len(tick_prices)

            # v7.0: Enhanced status line
            v60 = compute_velocity(60)
            vel_str = f"v60={v60:+.3f}%" if v60 is not None else "v60=n/a"
            pos_str = f"{local_pos.side[0].upper()}" if local_pos.in_position else "-"
            oco_tag = "SL" if local_pos.oco_attached else "L" if local_pos.in_position else ""
            lk = "LOCK" if locked else ""

            print(f"[{time.strftime('%H:%M:%S')}] ${price:,.2f} imb={ie:+.3f} {vel_str} pos={pos_str}{oco_tag} pnl=${daily_pnl:.2f} cd={cd}s hold={hold}s {lk}")

            # ══════════════════════════════════════════════════════════════
            # IN POSITION
            # ══════════════════════════════════════════════════════════════
            if local_pos.in_position:
                is_long = (local_pos.side == 'long')
                unrealized_pct = local_pos.unrealized_pct(price)

                # ── 1. HARD STOP BACKSTOP ──
                hard_stop_hit = False
                if is_long and price < local_pos.entry_price * (1 - HARD_STOP_PCT):
                    hard_stop_hit = True
                elif not is_long and price > local_pos.entry_price * (1 + HARD_STOP_PCT):
                    hard_stop_hit = True

                if hard_stop_hit:
                    print(f"  HARD STOP! {unrealized_pct*100:+.3f}% (limit {HARD_STOP_PCT*100:.2f}%)")
                    _side = local_pos.side; _entry = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct, fill = paper.execute_exit(_side, _entry, price, "hard_stop")
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        tg(f"HARD STOP ${fill:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close("hard_stop"); last_trade_time = now; unlock_entry()
                        consecutive_losses += 1; last_loss_time = now
                    else:
                        success = await close_position(signer, local_pos, "hard_stop", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            tg(f"HARD STOP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                            else: losses += 1; consecutive_losses += 1; last_loss_time = now
                            last_trade_time = now; unlock_entry()
                    save_state({"last_trade_time": now, "last_direction": last_direction,
                        "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
                        "daily_trades": daily_trades, "last_day": last_day,
                        "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                    await asyncio.sleep(3); continue

                # ── 2. SL CHECK (exchange SL or local) ──
                tp_sl = local_pos.check_tp_sl(price)
                # Skip TP if trailing is active
                if tp_sl == 'tp' and local_pos.trailing_active:
                    tp_sl = None

                if tp_sl == 'sl':
                    _side = local_pos.side; _entry = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct, fill = paper.execute_exit(_side, _entry, local_pos.sl_price, "sl")
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        tg(f"SL ${fill:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close("sl"); last_trade_time = now; unlock_entry()
                        consecutive_losses += 1; last_loss_time = now
                    elif local_pos.oco_attached:
                        if ws_is_flat():
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if fill else local_pos.sl_price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            print(f"  Exchange SL already filled @ ${exit_p:,.1f}")
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            tg(f"SL ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                            else: losses += 1; consecutive_losses += 1; last_loss_time = now
                            await cancel_stale_orders(signer)
                            local_pos.close("sl"); last_trade_time = now; unlock_entry()
                        else:
                            print(f"  SL crossed -- immediate market close")
                            success = await close_position(signer, local_pos, "fast_sl", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                                daily_pnl += pnl_usd; daily_trades += 1
                                track_direction_pnl(_side, pnl_usd)
                                tg(f"FAST SL ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                                if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                                else: losses += 1; consecutive_losses += 1; last_loss_time = now
                                last_trade_time = now; unlock_entry()
                    else:
                        print(f"  SL hit (no exchange SL), force closing!")
                        success = await close_position(signer, local_pos, "local_sl", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            tg(f"SL ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                            else: losses += 1; consecutive_losses += 1; last_loss_time = now
                            last_trade_time = now; unlock_entry()
                    if tp_sl == 'sl':
                        save_state({"last_trade_time": now, "last_direction": last_direction,
                            "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
                            "daily_trades": daily_trades, "last_day": last_day,
                            "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                        await asyncio.sleep(3); continue

                # ── 3. TRAILING STOP CHECK ──
                trail_result = local_pos.update_trailing_stop(price)
                if trail_result == 'trail_exit':
                    best_pct = abs(local_pos.best_price - local_pos.entry_price) / local_pos.entry_price * 100
                    print(f"  TRAIL EXIT! Best: ${local_pos.best_price:,.1f} (+{best_pct:.3f}%) -> stop at ${price:,.1f}")
                    _side = local_pos.side; _entry = local_pos.entry_price; _best = local_pos.best_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct, fill = paper.execute_exit(_side, _entry, price, "trail_exit")
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        tg(f"TRAIL EXIT ${fill:,.0f}\nBest: ${_best:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close("trail_exit"); last_trade_time = now; unlock_entry()
                        if pnl_usd >= 0: consecutive_losses = 0
                        else: consecutive_losses += 1; last_loss_time = now
                    else:
                        success = await close_position(signer, local_pos, "trail_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            tg(f"TRAIL EXIT ${exit_p:,.0f}\nBest: ${_best:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                            else: losses += 1; consecutive_losses += 1; last_loss_time = now
                            last_trade_time = now; unlock_entry()
                    save_state({"last_trade_time": now, "last_direction": last_direction,
                        "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
                        "daily_trades": daily_trades, "last_day": last_day,
                        "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                    await asyncio.sleep(3); continue

                # ── 4. MOMENTUM EXHAUSTION EXIT (NEW v7.0) ──
                if check_momentum_exhaustion(local_pos, price):
                    _side = local_pos.side; _entry = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct, fill = paper.execute_exit(_side, _entry, price, "mom_exhaust")
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        tg(f"MOM EXHAUST ${fill:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close("mom_exhaust"); last_trade_time = now; unlock_entry()
                        if pnl_usd >= 0: consecutive_losses = 0
                        else: consecutive_losses += 1; last_loss_time = now
                    else:
                        print(f"  Momentum exhaustion exit +{unrealized_pct*100:.3f}%")
                        success = await close_position(signer, local_pos, "mom_exhaust", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            tg(f"MOM EXHAUST ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                            else: losses += 1; consecutive_losses += 1; last_loss_time = now
                            last_trade_time = now; unlock_entry()
                    save_state({"last_trade_time": now, "last_direction": last_direction,
                        "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
                        "daily_trades": daily_trades, "last_day": last_day,
                        "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                    await asyncio.sleep(3); continue

                # ── 5. IMBALANCE EXIT (min 60s hold + 0.02% profit) ──
                if (unrealized_pct > IMB_EXIT_MIN_PROFIT and
                    not local_pos.trailing_active and
                    hold >= 60):
                    imb_against = (is_long and ie < -IMB_EXIT_THRESHOLD) or (not is_long and ie > IMB_EXIT_THRESHOLD)
                    if imb_against:
                        _side = local_pos.side; _entry = local_pos.entry_price
                        if PAPER_TRADE:
                            pnl_usd, pnl_pct, fill = paper.execute_exit(_side, _entry, price, "imb_exit")
                            daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                            tg(f"IMB EXIT ${fill:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                            local_pos.close("imb_exit"); last_trade_time = now; unlock_entry()
                            if pnl_usd >= 0: consecutive_losses = 0
                            else: consecutive_losses += 1; last_loss_time = now
                        else:
                            print(f"  Imb exit +{unrealized_pct*100:.3f}% hold={hold}s")
                            success = await close_position(signer, local_pos, "imb_exit", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                                daily_pnl += pnl_usd; daily_trades += 1
                                track_direction_pnl(_side, pnl_usd)
                                emoji = "WIN" if pnl_usd >= 0 else "LOSS"
                                tg(f"IMB {emoji} ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                                if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                                else: losses += 1; consecutive_losses += 1; last_loss_time = now
                                last_trade_time = now; unlock_entry()
                        save_state({"last_trade_time": now, "last_direction": last_direction,
                            "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
                            "daily_trades": daily_trades, "last_day": last_day,
                            "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                        await asyncio.sleep(3); continue

                # ── 6. TP CHECK (if not trailing) ──
                if tp_sl == 'tp':
                    _side = local_pos.side; _entry = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct, fill = paper.execute_exit(_side, _entry, local_pos.tp_price, "tp")
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        tg(f"TP ${fill:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%)\n{paper.summary()}")
                        local_pos.close("tp"); last_trade_time = now; unlock_entry()
                        consecutive_losses = 0
                    elif local_pos.oco_attached:
                        if ws_is_flat():
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if fill else local_pos.tp_price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            print(f"  TP confirmed via WS. Fill: ${exit_p:,.1f}")
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            tg(f"TP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                            else: losses += 1
                            await cancel_stale_orders(signer)
                            local_pos.close("tp"); last_trade_time = now; unlock_entry()
                        else:
                            print(f"  TP crossed, force closing to lock profit")
                            success = await close_position(signer, local_pos, "fast_tp", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                                daily_pnl += pnl_usd; daily_trades += 1
                                track_direction_pnl(_side, pnl_usd)
                                tg(f"TP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                                if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                                else: losses += 1
                                last_trade_time = now; unlock_entry()
                    else:
                        print(f"  TP hit (no exchange order), force closing!")
                        success = await close_position(signer, local_pos, "local_tp", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            tg(f"TP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                            else: losses += 1
                            last_trade_time = now; unlock_entry()
                    if tp_sl == 'tp':
                        save_state({"last_trade_time": now, "last_direction": last_direction,
                            "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
                            "daily_trades": daily_trades, "last_day": last_day,
                            "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                        await asyncio.sleep(3); continue

                # ── 7. TIME EXIT ──
                effective_max_hold = TRAIL_MAX_HOLD_SECS if local_pos.trailing_active else MAX_HOLD_SECS
                if hold > effective_max_hold:
                    _side = local_pos.side; _entry = local_pos.entry_price
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct, fill = paper.execute_exit(_side, _entry, price, "time_exit")
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        emoji = "WIN" if pnl_usd >= 0 else "LOSS"
                        tg(f"TIME {emoji} {hold}s ${pnl_usd:+.3f}\n{paper.summary()}")
                        local_pos.close("time_exit"); last_trade_time = now; unlock_entry()
                        if pnl_usd >= 0: consecutive_losses = 0
                        else: consecutive_losses += 1; last_loss_time = now
                    else:
                        print(f"  Time exit {hold}s")
                        success = await close_position(signer, local_pos, "time_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            emoji = "WIN" if pnl_usd >= 0 else "LOSS"
                            tg(f"TIME {emoji} {hold}s ${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1; consecutive_losses = 0
                            else: losses += 1; consecutive_losses += 1; last_loss_time = now
                            last_trade_time = now; unlock_entry()
                    save_state({"last_trade_time": now, "last_direction": last_direction,
                        "daily_pnl": daily_pnl, "wins": wins, "losses": losses,
                        "daily_trades": daily_trades, "last_day": last_day,
                        "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                    await asyncio.sleep(3); continue
                else:
                    oco_tag = "SL" if local_pos.oco_attached else "LOCAL"
                    tbias = tape_get_recent_bias(3)
                    tape_str = f" tape:{tbias:+.2f}" if abs(tbias) > 0.3 else ""
                    trail_str = f" trail=${local_pos.trail_stop_price:,.1f}" if local_pos.trailing_active else ""
                    print(f"  Holding {local_pos.side} ${local_pos.entry_price:,.0f} pnl={unrealized_pct*100:.3f}% {hold}s {oco_tag}{tape_str}{trail_str}")

            # ══════════════════════════════════════════════════════════════
            # ENTRY LOGIC (v7.0 — momentum/trend following)
            # ══════════════════════════════════════════════════════════════
            elif not local_pos.in_position and not locked:
                if not is_warmed_up():
                    warmup_left = int(WARMUP_SECS - (now - bot_start_time))
                    print(f"  Warming up... {warmup_left}s remaining")
                elif paused:
                    print("  Paused")
                elif daily_pnl <= -DAILY_LOSS_LIMIT:
                    print("  Daily loss limit")
                elif trades_this_hour >= MAX_TRADES_HOUR:
                    print("  Max trades/hr")
                elif cd > 0:
                    print(f"  CD {cd}s")
                # Loss streak cooldown
                elif consecutive_losses >= 3 and now - last_loss_time < LOSS_STREAK_COOLDOWN:
                    remaining = int(LOSS_STREAK_COOLDOWN - (now - last_loss_time))
                    print(f"  Loss streak CD ({consecutive_losses}L) {remaining}s")
                # Extra cooldown after any loss
                elif consecutive_losses > 0 and now - last_loss_time < LOSS_COOLDOWN_SECS:
                    remaining = int(LOSS_COOLDOWN_SECS - (now - last_loss_time))
                    print(f"  Loss CD {remaining}s")
                elif not volatility_ok(): pass
                elif not chop_filter(): pass
                elif not overextension_filter(): pass
                elif not spread_filter(): pass
                else:
                    direction, score = evaluate_entry()
                    if direction:
                        is_long_entry = (direction == 'long')
                        emoji = "LONG" if is_long_entry else "SHORT"

                        if PAPER_TRADE:
                            fill = paper.execute_entry(direction, price)
                            lock_entry(); local_pos.open(direction, fill)
                            last_trade_time = now; last_direction = direction; trades_this_hour += 1
                            tg(f"{emoji} ${fill:,.0f} score={score}\nSL:${local_pos.sl_price:,.0f}\n"
                               f"v60={compute_velocity(60):+.3f}% tape30={compute_tape_momentum(30):+.2f}")
                            save_state({"last_trade_time": now,
                                "last_direction": direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day,
                                "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                            await asyncio.sleep(5); continue
                        else:
                            # Check WS for existing position
                            if not ws_is_flat():
                                print(f"  Entry blocked: WS shows position open")
                                lock_entry()
                                await asyncio.sleep(10); continue

                            lock_entry()
                            v60_val = compute_velocity(60)
                            t30_val = compute_tape_momentum(30)
                            print(f"  {emoji} ${price:,.0f} score={score} v60={v60_val:+.3f}% tape30={t30_val:+.2f} imb={ie:+.3f}")
                            await cancel_stale_orders(signer)
                            _, _, err = await signer.create_market_order_limited_slippage(
                                market_index=MARKET_INDEX, client_order_index=0,
                                base_amount=BASE_AMOUNT, max_slippage=0.005,
                                is_ask=0 if is_long_entry else 1)
                            if err:
                                tg(f"Order failed: {err}"); unlock_entry()
                            else:
                                local_pos.open(direction, price)
                                last_trade_time = now; last_direction = direction; trades_this_hour += 1
                                tg(f"{emoji} ${price:,.0f} score={score}\nSL:${local_pos.sl_price:,.0f}\n"
                                   f"v60={v60_val:+.3f}% tape30={t30_val:+.2f} imb={ie:+.3f}")
                                save_state({"last_trade_time": now,
                                    "last_direction": direction, "daily_pnl": daily_pnl,
                                    "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day,
                                    "consecutive_losses": consecutive_losses, "last_loss_time": last_loss_time})
                                asyncio.create_task(attach_oco_background(signer, is_long_entry, price))
                                await asyncio.sleep(10); continue
                    else:
                        # No signal — show current readings
                        v60_d = compute_velocity(60)
                        t30_d = compute_tape_momentum(30)
                        v_str = f"v60={v60_d:+.3f}%" if v60_d is not None else "v60=n/a"
                        print(f"  Scanning {v_str} tape30={t30_d:+.2f} imb={ie:+.3f}")

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
