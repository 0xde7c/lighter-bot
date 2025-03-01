#!/usr/bin/env python3
"""
Scalp Bot v6.5.7 — FIX CLOSE ORDER (limited_slippage)
  v6.5.4 changes from v6.5.3:
  1. Trend filter: block shorts in uptrends, longs in downtrends (5min)
  2. Position sync: detect & fix naked positions every loop iteration
  3. SL BaseAmount fix (was 0, now BASE_AMOUNT)
  4. Removed duplicate config block
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

# ── STRATEGY PARAMS ─────────────────────────────────────────────────────
TP_PCT=0.0005        # 0.05% TP — only used for OCO, trailing stop takes over in profit
SL_PCT=0.0004        # 0.04% SL — tighter cut, avg win ~$0.14
HARD_STOP_PCT=0.0006 # 0.06% — tighter backstop behind 0.04% SL

# ── TRAILING STOP (v6.5.3) ──────────────────────────────────────────────
TRAIL_ACTIVATE_PCT=0.0004  # 0.03% — activate trailing stop after this profit
TRAIL_DISTANCE_PCT=0.0003 # 0.025% — trail this far behind best price
TRAIL_MAX_HOLD_SECS=90     # extended hold time when trailing (momentum needs time)

MAX_HOLD_SECS=60     # reduced from 60 — cut losers faster (extended to 90 when trailing)
MIN_TRADE_INTERVAL=20
MAX_TRADES_HOUR=60
DAILY_LOSS_LIMIT=2.0
SAME_DIR_COOLDOWN=15

# ── SIGNALS ─────────────────────────────────────────────────────────────
IMB_LEVELS=5; IMB_EMA_ALPHA=0.1
IMB_ENTRY_THRESHOLD=0.38
IMB_EXIT_THRESHOLD=0.15
IMB_EXIT_MIN_PROFIT=0.0002  # 0.02% — take smaller profits faster with tighter SL
VAMP_MIN_DIVERGENCE=0.0
SMOOTH_TICKS=10; LOOKBACK_TICKS=30; MOM_BLOCK_THRESHOLD=0.0
PERCENTILE_LOOKBACK=300; PERCENTILE_UPPER=92; PERCENTILE_LOWER=8

# ── VOLATILITY FILTER ───────────────────────────────────────────────────
VOL_LOOKBACK_SECS=300
VOL_MIN_RANGE_PCT=0.10

# ── CHOP FILTER (v6.5.2) ───────────────────────────────────────────────
CHOP_LOOKBACK_SECS=900   # 15 minutes
CHOP_MIN_RANGE_PCT=0.15  # need 0.15% range over 15min to trade

# ── TREND FILTER (v6.5.4) ──────────────────────────────────────────────
TREND_LOOKBACK_SECS=300   # 5 minutes
TREND_MIN_PCT=0.0008      # 0.08% — block counter-trend entries
TREND_EXTENDED_PCT=0.0020 # 0.20% — block with-trend entries (buying tops / selling bottoms)

# ── SIMPLIFIED EXIT ─────────────────────────────────────────────────────
EXIT_SETTLE_SECS=5   # seconds to wait after exit order before clearing state
LOCK_DURATION=105    # covers trail_hold(90) + settle(8) + buffer

STATE_FILE="/root/rsi_bot/.bot_state_v657paper.json"
LOCK_FILE="/root/rsi_bot/.entry_lock_v657paper"
LOCAL_POS_FILE="/root/rsi_bot/.local_position_v657paper.json"

tick_prices=collections.deque(maxlen=1200); tick_lock=threading.Lock()
ob_bids=[]; ob_asks=[]; ob_lock=threading.Lock()
imb_ema=0.0; imb_ema_lock=threading.Lock()
momentum_history=collections.deque(maxlen=1200)

# v6.5: Account websocket state
acct_ws_lock = threading.Lock()
acct_ws_position = {"size": 0.0, "sign": 0, "entry": 0.0, "open_orders": 0}  # live position from WS
acct_ws_balance = 0.0  # live balance from WS
acct_ws_last_trade = None  # last trade from WS: {"price": float, "size": float, "side": str, "time": float}
acct_ws_ready = threading.Event()  # set once first snapshot received
acct_ws_trade_queue = collections.deque(maxlen=50)  # recent trades from WS for PnL matching

# v6.5.1: Trade tape state (market-wide fills from trade/1)
tape_lock = threading.Lock()
tape_recent = collections.deque(maxlen=200)  # recent market trades
tape_whale_threshold = 0.5  # BTC — log fills above this
tape_last_whale = None  # last whale trade for signal logging


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
        print(f"  ⚠️ API error: {e}")
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
                    print(f"  📂 Loaded: {self.side} @ ${self.entry_price:,.1f} oco={self.oco_attached}")
        except: pass

    def _save(self):
        try:
            with open(LOCAL_POS_FILE, "w") as f:
                json.dump({k: getattr(self, k) for k in
                    ['in_position','side','entry_price','entry_time','size',
                     'sl_price','tp_price','oco_attached']}, f)
        except Exception as e:
            print(f"  ⚠️ Save error: {e}")

    def open(self, side, entry_price, size=0.005):
        self.in_position = True
        self.side = side
        self.entry_price = entry_price
        self.entry_time = time.time()
        self.size = size
        self.oco_attached = False
        # v6.5.4: Trend filter + position sync state
        self.trailing_active = False
        self.best_price = entry_price  # best price seen since entry
        self.trail_stop_price = 0.0    # trailing stop trigger price
        if side == 'long':
            self.tp_price = entry_price * (1 + TP_PCT)
            self.sl_price = entry_price * (1 - SL_PCT)
        else:
            self.tp_price = entry_price * (1 - TP_PCT)
            self.sl_price = entry_price * (1 + SL_PCT)
        self._save()
        print(f"  📝 OPENED {side} @ ${entry_price:,.1f} TP=${self.tp_price:,.1f} SL=${self.sl_price:,.1f}")

    def close(self, reason=""):
        side = self.side; entry = self.entry_price
        self.in_position = False; self.side = None; self.entry_price = 0.0
        self.entry_time = 0.0; self.size = 0.0; self.sl_price = 0.0
        self.tp_price = 0.0; self.oco_attached = False
        self._save()
        print(f"  📝 CLOSED {side} from ${entry:,.1f} ({reason})")

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
        """v6.5.3: Update trailing stop. Returns 'trail_exit' if triggered, None otherwise."""
        if not self.in_position: return None
        profit_pct = self.unrealized_pct(current_price)
        
        # Activate trailing stop once we hit the threshold
        if not self.trailing_active and profit_pct >= TRAIL_ACTIVATE_PCT:
            self.trailing_active = True
            self.best_price = current_price
            if self.side == 'long':
                self.trail_stop_price = current_price * (1 - TRAIL_DISTANCE_PCT)
            else:
                self.trail_stop_price = current_price * (1 + TRAIL_DISTANCE_PCT)
            print(f"  🔄 TRAIL ACTIVATED at +{profit_pct*100:.3f}% | stop=${self.trail_stop_price:,.1f}")
            return None
        
        if not self.trailing_active: return None
        
        # Update best price and trail stop
        if self.side == 'long':
            if current_price > self.best_price:
                self.best_price = current_price
                self.trail_stop_price = current_price * (1 - TRAIL_DISTANCE_PCT)
            # Check if trail stop triggered
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

            # ── ACCOUNT UPDATE (v6.5) ──
            elif "account_all" in channel or msg_type in ("update/account_all", "update/account"):
                with acct_ws_lock:
                    # Parse positions
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
                    
                    # Parse assets/balance
                    assets = data.get("assets", {})
                    usdc = assets.get("3") or assets.get("0")  # USDC asset
                    if usdc:
                        acct_ws_balance = float(usdc.get("balance", "0"))

                    # Parse trades (for fill price tracking)
                    trades = data.get("trades", {})
                    market_trades = trades.get(str(MARKET_INDEX)) or trades.get("1")
                    if market_trades:
                        if isinstance(market_trades, dict):
                            market_trades = [market_trades]
                        for t in market_trades:
                            trade_info = {
                                "price": float(t.get("price", "0")),
                                "size": float(t.get("size", "0")),
                                "side": t.get("type", ""),  # "buy" or "sell"
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
                                print(f"  📡 WS Trade: {trade_info['side']} {trade_info['size']:.5f} @ ${trade_info['price']:,.1f}")
                    
                    acct_ws_ready.set()

            # ── TRADE TAPE (v6.5.1) — market-wide fills ──
            elif "trade:" in channel and "account" not in channel:
                tape_trades = data.get("trades", [])
                if isinstance(tape_trades, dict):
                    tape_trades = [tape_trades]
                with tape_lock:
                    for t in tape_trades:
                        size = float(t.get("size", "0"))
                        price = float(t.get("price", "0"))
                        side = t.get("type", "")  # "buy" or "sell"
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
                        
                        # Log whale orders
                        if size >= tape_whale_threshold:
                            tape_last_whale = trade_info
                            print(f"  🐋 WHALE {side.upper()} {size:.4f} BTC (${trade_info['usd']:,.0f}) @ ${price:,.1f}")
                        
                        # Match our account fills for exact PnL
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
                                print(f"  📡 OUR FILL: {side} {size:.5f} @ ${price:,.1f}")

        except Exception as e:
            pass  # Don't crash WS thread on parse errors

    def on_close(ws, *a): time.sleep(2); run()
    def on_open(ws):
        print("  WS connected")
        ws.send(json.dumps({"type": "subscribe", "channel": "order_book/1"}))
        ws.send(json.dumps({"type": "subscribe", "channel": f"account_all/{ACCOUNT_INDEX}"}))
        ws.send(json.dumps({"type": "subscribe", "channel": "trade/1"}))
        print(f"  📡 Subscribed to account_all/{ACCOUNT_INDEX} + trade/1")
    def run():
        try:
            ws_lib.WebSocketApp("wss://mainnet.zklighter.elliot.ai/stream",
                on_message=on_msg, on_close=on_close, on_open=on_open
            ).run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"  WS error: {e}"); time.sleep(2); run()
    run()


# ── SIGNALS ─────────────────────────────────────────────────────────────
def get_imb_ema():
    with imb_ema_lock: return imb_ema

def get_vamp():
    with ob_lock: bids = list(ob_bids[:IMB_LEVELS]); asks = list(ob_asks[:IMB_LEVELS])
    if not bids or not asks: return 0, 0
    bp, bq = bids[0]; ap, aq = asks[0]; mid = (bp + ap) / 2; tq = bq + aq
    if tq == 0: return mid, 0
    vamp = (bp * aq + ap * bq) / tq
    return vamp, (vamp - mid) / mid

def get_momentum():
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < LOOKBACK_TICKS + SMOOTH_TICKS: return None, None
    current = sum(t[1] for t in ticks[-SMOOTH_TICKS:]) / SMOOTH_TICKS
    pe = len(ticks) - LOOKBACK_TICKS; ps = max(0, pe - SMOOTH_TICKS)
    past = sum(t[1] for t in ticks[ps:pe]) / (pe - ps)
    mom = (current - past) / past; momentum_history.append(mom)
    return current, mom

def percentile_allows(direction):
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < PERCENTILE_LOOKBACK: return True
    recent = sorted([t[1] for t in ticks[-PERCENTILE_LOOKBACK:]]); cur = ticks[-1][1]
    idx = sum(1 for p in recent if p <= cur); pctile = (idx / len(recent)) * 100
    if direction == 'short' and pctile > PERCENTILE_UPPER:
        print(f"  ⛔ Anti-top: {pctile:.0f}th pctile"); return False
    if direction == 'long' and pctile < PERCENTILE_LOWER:
        print(f"  ⛔ Anti-bottom: {pctile:.0f}th pctile"); return False
    return True

def volatility_ok():
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < 30: return True
    now = time.time()
    recent = [t[1] for t in ticks if now - t[0] <= VOL_LOOKBACK_SECS]
    if len(recent) < 10: return True
    high = max(recent); low = min(recent)
    range_pct = (high - low) / low * 100
    if range_pct < VOL_MIN_RANGE_PCT:
        print(f"  💤 Low vol: {range_pct:.3f}% (need {VOL_MIN_RANGE_PCT}%)")
        return False
    return True

def chop_filter_ok():
    """v6.5.2: Block trading when price is in tight range over 15min."""
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < 60: return True  # not enough data yet
    now = time.time()
    recent = [t[1] for t in ticks if now - t[0] <= CHOP_LOOKBACK_SECS]
    if len(recent) < 30: return True  # not enough 15min data
    high = max(recent); low = min(recent)
    range_pct = (high - low) / low * 100
    if range_pct < CHOP_MIN_RANGE_PCT:
        print(f"  🔀 Chop: {range_pct:.3f}% 15m range (need {CHOP_MIN_RANGE_PCT}%)")
        return False
    return True


def trend_filter(direction):
    """v6.5.7: Block counter-trend at 0.08%, block with-trend (extended) at 0.20%."""
    with tick_lock: ticks = list(tick_prices)
    if len(ticks) < 60: return True
    now = time.time()
    recent = [t for t in ticks if now - t[0] <= TREND_LOOKBACK_SECS]
    if len(recent) < 30: return True
    old_price = recent[0][1]
    new_price = recent[-1][1]
    trend_pct = (new_price - old_price) / old_price
    # Block counter-trend (tight threshold — don't fight the trend)
    if direction == 'short' and trend_pct > TREND_MIN_PCT:
        print(f"  ⬆️ Trend: +{trend_pct*100:.3f}% 5m (blocking short — counter-trend)")
        return False
    if direction == 'long' and trend_pct < -TREND_MIN_PCT:
        print(f"  ⬇️ Trend: {trend_pct*100:.3f}% 5m (blocking long — counter-trend)")
        return False
    # Block with-trend when extended (loose threshold — don't buy tops / sell bottoms)
    if direction == 'long' and trend_pct > TREND_EXTENDED_PCT:
        print(f"  ⬆️ Trend: +{trend_pct*100:.3f}% 5m (blocking long — buying top)")
        return False
    if direction == 'short' and trend_pct < -TREND_EXTENDED_PCT:
        print(f"  ⬇️ Trend: {trend_pct*100:.3f}% 5m (blocking short — selling bottom)")
        return False
    return True


# ── PNL CALCULATION (v6.4.5: from prices, not balance deltas) ──────────
def calc_pnl_from_prices(side, entry_price, exit_price):
    """Calculate PnL in USD from entry/exit prices. No API dependency."""
    if side == 'long':
        pnl_pct = (exit_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - exit_price) / entry_price
    notional = (BASE_AMOUNT / 100000) * entry_price  # 500 base = 0.005 BTC * price
    pnl_usd = pnl_pct * notional
    return pnl_usd, pnl_pct


# ── v6.5: ACCOUNT WS HELPERS ──────────────────────────────────────────
def ws_get_position():
    """Get current position from WS feed. Returns dict like api_get_position."""
    with acct_ws_lock:
        return dict(acct_ws_position)

def ws_is_flat():
    """Check if position is flat (no open position) via WS."""
    with acct_ws_lock:
        return acct_ws_position["size"] == 0.0

def ws_get_last_fill():
    """Get the most recent fill from WS trade queue."""
    with acct_ws_lock:
        return dict(acct_ws_last_trade) if acct_ws_last_trade else None

def ws_get_balance():
    """Get current balance from WS feed."""
    with acct_ws_lock:
        return acct_ws_balance

async def ws_wait_for_flat(timeout=10):
    """Wait for position to become flat via WS, with timeout. Returns True if flat."""
    start = time.time()
    while time.time() - start < timeout:
        if ws_is_flat():
            return True
        await asyncio.sleep(0.2)
    return False

async def ws_wait_for_position(timeout=10):
    """Wait for position to appear via WS, with timeout. Returns True if position exists."""
    start = time.time()
    while time.time() - start < timeout:
        pos = ws_get_position()
        if pos["size"] > 0:
            return True
        await asyncio.sleep(0.2)
    return False


async def position_sync_check(signer, local_pos):
    """v6.5.4: Detect and fix position mismatches every loop."""
    ws_pos = ws_get_position()
    
    # Bot thinks flat, but exchange has position → NAKED POSITION
    if not local_pos.in_position and ws_pos["size"] > 0:
        is_long = ws_pos["sign"] > 0
        side_str = "LONG" if is_long else "SHORT"
        print(f"  🚨 SYNC: Naked {side_str} detected! Size={ws_pos['size']:.5f} @ ${ws_pos['entry']:,.1f}")
        tg(f"🚨 NAKED {side_str} detected!\nSize: {ws_pos['size']:.5f} @ ${ws_pos['entry']:,.1f}\n🔄 Auto-closing...")
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
                    print(f"  ✅ SYNC: Naked position closed")
                    tg(f"✅ Naked position auto-closed")
                else:
                    print(f"  ⚠️ SYNC: Close sent but still open")
                    tg(f"⚠️ Naked close may have failed — check manually!")
            else:
                print(f"  ❌ SYNC: Close failed: {err}")
                tg(f"❌ Naked close failed: {err}\n⚠️ Close manually!")
        except Exception as e:
            print(f"  ❌ SYNC: Exception: {e}")
            tg(f"❌ Naked close error: {e}\n⚠️ Close manually!")
        return True
    
    # Bot thinks in_position but exchange is flat → sync local
    if local_pos.in_position and ws_pos["size"] == 0:
        rest_pos = api_get_position()
        if rest_pos and rest_pos["size"] == 0:
            print(f"  ⚠️ SYNC: Bot thinks {local_pos.side} but exchange flat — syncing")
            tg(f"⚠️ SYNC: Position gone\nWas: {local_pos.side} @ ${local_pos.entry_price:,.0f}")
            local_pos.close("sync_flat")
            return True
    
    return False


# v6.5.1: Trade tape helpers
def tape_get_recent_volume(seconds=5):
    """Get total BTC volume from tape in last N seconds."""
    with tape_lock:
        cutoff = time.time() - seconds
        return sum(t["size"] for t in tape_recent if t["time"] > cutoff)

def tape_get_recent_bias(seconds=5):
    """Get buy/sell bias from tape. >0 = more buying, <0 = more selling."""
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
    d = {"last_trade_time": 0, "last_dir_close_time": 0, "last_direction": None,
         "daily_pnl": 0, "wins": 0, "losses": 0, "daily_trades": 0, "last_day": time.strftime("%d")}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            for k, v in d.items():
                if k not in data: data[k] = v
            return data
    except: return d

def tg(msg):
    prefix = "📝 " if PAPER_TRADE else ""
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": prefix + msg, "parse_mode": "HTML"}, timeout=5)
    except: pass


# ══════════════════════════════════════════════════════════════════════════
# OCO MANAGEMENT
# v6.5.3: Only attach SL on exchange. TP is handled by bot's trailing stop.
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
            base_amount=BASE_AMOUNT,  # must be >0 for Lighter
            price=sl_l,
            is_ask=ea,
            trigger_price=sl_t
        )
        return err, sl_p
    except Exception as e:
        # Fallback: try using create_order directly
        try:
            sl_o = CreateOrderTxReq(MarketIndex=MARKET_INDEX, ClientOrderIndex=0, BaseAmount=BASE_AMOUNT, Price=sl_l, IsAsk=ea,
                Type=signer.ORDER_TYPE_STOP_LOSS_LIMIT, TimeInForce=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                ReduceOnly=1, TriggerPrice=sl_t, OrderExpiry=-1)
            _, _, err = await signer.create_grouped_orders(grouping_type=signer.GROUPING_TYPE_ONE_CANCELS_THE_OTHER, orders=[sl_o])
            return err, sl_p
        except Exception as e2:
            return str(e2), 0

async def attach_oco_background(signer, is_long, entry_price):
    """v6.5.7: Attach SL with staleness check — skip if position changed."""
    expected_entry_time = local_pos.entry_time
    await asyncio.sleep(2)
    # v6.5.7: If position changed since we started, abort — prevents stale SL killing new trade
    if local_pos.entry_time != expected_entry_time:
        print(f"  ⚠️ SL skipped — position changed (stale task)")
        return
    for attempt in range(3):
        # Check again before each attempt
        if local_pos.entry_time != expected_entry_time:
            print(f"  ⚠️ SL skipped — position changed (stale task)")
            return
        try:
            err, sl_p = await attach_sl_order(signer, entry_price, is_long)
            if not err:
                local_pos.oco_attached = True; local_pos._save()
                tg(f"✅ SL:${sl_p:,.0f} (trailing stop manages TP)")
                return
            print(f"  SL attempt {attempt+1}: {err}")
        except Exception as e:
            print(f"  SL attempt {attempt+1}: {e}")
        await asyncio.sleep(2)
    # SL FAILED — bot will monitor SL locally
    if local_pos.entry_time == expected_entry_time:
        local_pos.oco_attached = False; local_pos._save()
        tg("⚠️ SL order failed — bot monitoring SL locally")


# ══════════════════════════════════════════════════════════════════════════
# SIMPLIFIED EXIT — trust the order, not the API
# ══════════════════════════════════════════════════════════════════════════
async def send_market_close(signer, is_long):
    """Send market close order with ReduceOnly to prevent opening reverse position."""
    try:
        # Cancel any existing orders first (timestamp_ms=0 for IMMEDIATE)
        await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
        await asyncio.sleep(1)
    except Exception as e:
        print(f"  ⚠️ Cancel error: {e}")

    # v6.5.7: Use create_market_order_limited_slippage for closes (fixes price encoding bug)
    # Old create_market_order passed raw float price (66300) but SDK expects encoded int (663000)
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
        print(f"  ⚠️ ReduceOnly failed ({e}), checking WS...")
        if ws_is_flat():
            print(f"  ✅ WS shows flat — position already closed")
            return True
        # v6.5.3: NEVER use non-ReduceOnly fallback — that causes naked positions
        # Instead, alert user to close manually
        print(f"  🚨 ReduceOnly failed and position still open!")
        tg(f"🚨 ReduceOnly close failed!\nPosition still open — close manually!\n{e}")
        err = str(e)

    if err:
        print(f"  ❌ Exit order failed: {err}")
        tg(f"⚠️ Exit failed: {err}")
        return False
    return True

async def cancel_stale_orders(signer):
    """v6.5.7: Cancel ALL open orders after closing. Prevents stale SL from killing next trade."""
    try:
        await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
        print(f"  🧹 Cancelled stale orders")
    except Exception as e:
        print(f"  ⚠️ Stale order cancel failed: {e}")

async def close_position(signer, local_pos, reason, price):
    """
    v6.5: Send close order, then wait for WS to confirm position is flat.
    Falls back to timed wait if WS doesn't confirm.
    """
    is_long = (local_pos.side == 'long')
    entry = local_pos.entry_price

    if PAPER_TRADE:
        return True

    print(f"  🔄 Closing: {reason}")
    success = await send_market_close(signer, is_long)

    if success:
        # v6.5: Wait for WS to confirm position is flat
        print(f"  ⏳ Waiting for WS position confirmation...")
        flat = await ws_wait_for_flat(timeout=8)
        if flat:
            print(f"  ✅ WS confirmed: position flat")
            await cancel_stale_orders(signer)
            local_pos.close(reason)
            return True
        else:
            # v6.5.2: Don't assume closed — verify via REST API
            print(f"  ⚠️ WS timeout — checking REST API...")
            pos = api_get_position()
            if pos and pos["size"] == 0:
                print(f"  ✅ REST confirms: position flat")
                await cancel_stale_orders(signer)
                local_pos.close(reason)
                return True
            elif pos and pos["size"] > 0:
                print(f"  ⚠️ REST shows position still open! Waiting 3s then re-checking...")
                # Wait for first close to potentially fill
                await asyncio.sleep(3)
                # Re-check WS (more reliable than REST for real-time state)
                if ws_is_flat():
                    print(f"  ✅ WS now shows flat — first close filled late")
                    local_pos.close(reason)
                    return True
                # Still not flat — check REST one more time
                pos2 = api_get_position()
                if pos2 and pos2["size"] == 0:
                    print(f"  ✅ REST now confirms flat")
                    local_pos.close(reason)
                    return True
                # Truly still open — retry close
                print(f"  ⚠️ Still open after 3s — retrying close with ReduceOnly...")
                success2 = await send_market_close(signer, is_long)
                if success2:
                    flat2 = await ws_wait_for_flat(timeout=8)
                    if flat2:
                        print(f"  ✅ WS confirmed flat on retry")
                    else:
                        print(f"  ⚠️ Still not confirmed — check manually")
                        tg(f"⚠️ Exit uncertain — check position!")
                local_pos.close(reason)
                return True
            else:
                print(f"  ⚠️ REST API failed — assuming closed")
                local_pos.close(reason)
                return True
    else:
        # Order failed — try once more
        print(f"  🔄 Retry exit...")
        await asyncio.sleep(2)
        success2 = await send_market_close(signer, is_long)
        if success2:
            flat = await ws_wait_for_flat(timeout=8)
            if flat:
                print(f"  ✅ WS confirmed: position flat")
            else:
                # Verify via REST
                pos = api_get_position()
                if pos and pos["size"] > 0:
                    print(f"  ⚠️ REST shows still open after retry!")
                    tg(f"🚨 Exit may have failed — check position!")
                else:
                    print(f"  ✅ REST confirms flat on retry")
            local_pos.close(reason)
            return True
        else:
            tg(f"🚨 EXIT FAILED TWICE! Close manually!\n{local_pos.side} @ ${entry:,.0f}")
            return False


# ══════════════════════════════════════════════════════════════════════════
# v6.4.4 NEW: STARTUP POSITION CLEANUP
# ══════════════════════════════════════════════════════════════════════════
async def startup_close_orphan(signer):
    """
    On startup, check if there's an open position on the exchange.
    If found, close it immediately before starting the trading loop.
    This prevents opening new trades on top of existing ones after a restart.
    """
    print("  🔍 Checking for orphaned positions...")
    
    for attempt in range(3):
        pos = api_get_position()
        if pos is None:
            print(f"  ⚠️ API failed (attempt {attempt+1}/3), retrying...")
            await asyncio.sleep(2)
            continue
        
        if pos["size"] == 0 and pos["open_orders"] == 0:
            print(f"  ✅ No orphaned position. Balance: ${pos['balance']:.2f}")
            return pos["balance"]
        
        if pos["open_orders"] > 0:
            print(f"  🧹 Cancelling {pos['open_orders']} orphaned orders...")
            try:
                await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"  ⚠️ Cancel error: {e}")
        
        if pos["size"] > 0:
            is_long = (pos["sign"] > 0)
            side_str = "LONG" if is_long else "SHORT"
            print(f"  🚨 Orphaned {side_str} position: {pos['size']:.5f} BTC @ ${pos['entry']:,.1f}")
            print(f"  🔄 Auto-closing orphaned position...")
            tg(f"🚨 Orphaned {side_str} found on startup\nSize: {pos['size']:.5f} @ ${pos['entry']:,.1f}\n🔄 Auto-closing...")
            
            # Send market close — use ReduceOnly for safety
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
                print(f"  🚨 ReduceOnly orphan close failed ({e})")
                tg(f"🚨 Orphan close failed!\n{e}\n⚠️ Close manually on Lighter!")
                # Don't use non-ReduceOnly fallback — that creates naked positions
                err = str(e)
            
            if err:
                print(f"  ❌ Close failed: {err}")
                tg(f"❌ Failed to close orphan: {err}\n⚠️ Close manually!")
                # Don't return None — try again
                await asyncio.sleep(3)
                continue
            
            print(f"  ⏳ Waiting for settlement...")
            await asyncio.sleep(EXIT_SETTLE_SECS + 2)
            
            # Verify it closed
            pos2 = api_get_position()
            if pos2 and pos2["size"] == 0:
                print(f"  ✅ Orphaned position closed. Balance: ${pos2['balance']:.2f}")
                tg(f"✅ Orphaned position closed\nBalance: ${pos2['balance']:.2f}")
                return pos2["balance"]
            else:
                print(f"  ⚠️ Position may still be open, retrying...")
                await asyncio.sleep(2)
                continue
    
    # All attempts failed
    print(f"  🚨 Could not clear orphaned position after 3 attempts!")
    tg(f"🚨 STARTUP FAILED: Could not close orphaned position!\nClose manually and restart.")
    return None


# ── GLOBALS ─────────────────────────────────────────────────────────────
state = load_state()
# v6.4.4: Reset PnL tracking on each restart to avoid ghost PnL
daily_pnl = 0.0; daily_trades = 0
trades_this_hour = 0; hour_reset_time = time.time()
last_trade_time = state["last_trade_time"]
wins = 0; losses = 0
long_pnl = 0.0; short_pnl = 0.0; long_trades = 0; short_trades = 0  # v6.5.2: direction tracking
last_update_id = 0; paused = False; last_day = state["last_day"]

def track_direction_pnl(side, pnl):
    """v6.5.2: Track PnL by direction for analysis."""
    global long_pnl, short_pnl, long_trades, short_trades
    if side == 'long':
        long_pnl += pnl; long_trades += 1
    else:
        short_pnl += pnl; short_trades += 1
last_direction = state["last_direction"]; last_dir_close_time = state["last_dir_close_time"]

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
            if msg == "/pause": paused = True; tg("⏸ Paused")
            elif msg == "/resume": paused = False; tg("▶️ Resumed")
            elif msg == "/status":
                price, mom = get_momentum()
                ie = get_imb_ema()
                pos_str = f"{local_pos.side} @ ${local_pos.entry_price:,.0f}" if local_pos.in_position else "flat"
                oco_str = "OCO✅" if local_pos.oco_attached else "OCO❌" if local_pos.in_position else ""
                mode = "📝PAPER" if PAPER_TRADE else "💰LIVE"
                wr = f"{wins/(wins+losses)*100:.0f}%" if wins+losses > 0 else "N/A"
                tg(f"📊 v6.5.7 [{mode}]\n${price:,.0f} | Imb:{ie:+.2f}\n"
                   f"Pos: {pos_str} {oco_str}\n"
                   f"Day: ${daily_pnl:.2f} | {wins}W/{losses}L ({wr})\n"
                   f"L: {long_trades}t ${long_pnl:+.2f} | S: {short_trades}t ${short_pnl:+.2f}\n"
                   f"TP:0.05% SL:0.05% Hold:{MAX_HOLD_SECS}s")
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
    global wins, losses, last_day, paused, last_direction, last_dir_close_time

    mode_str = "📝 PAPER" if PAPER_TRADE else "💰 LIVE"
    print(f"Scalp Bot v6.5.7 [{mode_str}]")
    print(f"  TP:{TP_PCT*100:.2f}% SL:{SL_PCT*100:.2f}% HardStop:{HARD_STOP_PCT*100:.2f}%")
    print(f"  Trail: activate@{TRAIL_ACTIVATE_PCT*100:.2f}% distance:{TRAIL_DISTANCE_PCT*100:.3f}% max_hold:{TRAIL_MAX_HOLD_SECS}s")
    print(f"  Hold:{MAX_HOLD_SECS}s CD:{MIN_TRADE_INTERVAL}s DirCD:{SAME_DIR_COOLDOWN}s")
    print(f"  Vol filter: {VOL_MIN_RANGE_PCT}% 5min | Chop filter: {CHOP_MIN_RANGE_PCT}% 15min")
    print(f"  v6.5.7: Fixed close orders (limited_slippage) + trend filter + sync")

    threading.Thread(target=ws_thread, daemon=True).start()
    await asyncio.sleep(3)
    # v6.5: Wait for account WS snapshot
    if not PAPER_TRADE:
        print("  ⏳ Waiting for account WS snapshot...")
        acct_ws_ready.wait(timeout=10)
        if acct_ws_ready.is_set():
            print(f"  ✅ Account WS ready. Balance: ${ws_get_balance():.2f}")
        else:
            print(f"  ⚠️ Account WS not ready — continuing with REST fallback")

    signer = None
    if not PAPER_TRADE:
        signer = lighter.SignerClient(url=LIGHTER_URL, account_index=ACCOUNT_INDEX,
            api_private_keys={API_KEY_INDEX: API_PRIVATE_KEY})
        signer.create_client(api_key_index=API_KEY_INDEX)

    # ══════════════════════════════════════════════════════════════════
    # v6.4.4: STARTUP SAFETY — clear any orphaned positions
    # ══════════════════════════════════════════════════════════════════
    # Always clear local position file on restart
    if local_pos.in_position:
        print(f"  ⚠️ Stale local position — clearing file")
        local_pos.close("stale_startup")
    unlock_entry()

    starting_bal = None
    if not PAPER_TRADE:
        # Auto-close any orphaned positions on the exchange
        starting_balance = await startup_close_orphan(signer)
        if starting_balance is None:
            print("  🚨 ABORTING: Could not ensure clean state on startup")
            tg("🚨 Bot aborted — could not close orphaned position. Fix manually.")
            return
        starting_bal = starting_balance
    
    tg(f"🚀 v6.5.7 [{mode_str}]\nSL:{SL_PCT*100:.2f}% HS:{HARD_STOP_PCT*100:.2f}%\n"
       f"Trail: @{TRAIL_ACTIVATE_PCT*100:.2f}% dist:{TRAIL_DISTANCE_PCT*100:.3f}%\n"
       f"Hold:{MAX_HOLD_SECS}s (trail:{TRAIL_MAX_HOLD_SECS}s)\n"
       f"🆕 Trailing stop — let winners run"
       + (f"\nPaper: ${paper.balance:.2f}" if PAPER_TRADE else f"\nBalance: ${starting_bal:.2f}"))

    last_tg_check = 0; warmup_ticks = 50

    while True:
        try:
            now = time.time()
            if now - hour_reset_time > 3600: trades_this_hour = 0; hour_reset_time = now
            today = time.strftime("%d")
            if today != last_day:
                daily_pnl = 0.0; daily_trades = 0; wins = 0; losses = 0; last_day = today
                if PAPER_TRADE and paper: paper.daily_pnl = 0.0
                tg("🌅 Daily reset")
                save_state({"last_trade_time": last_trade_time, "last_dir_close_time": last_dir_close_time,
                    "last_direction": last_direction, "daily_pnl": 0, "wins": 0, "losses": 0,
                    "daily_trades": 0, "last_day": today})
            if now - last_tg_check > 3: tg_check(); last_tg_check = now

            price_mom = get_momentum()
            if price_mom[0] is None: await asyncio.sleep(1); continue
            price, momentum = price_mom
            ie = get_imb_ema(); _, vamp_d = get_vamp()

            # v6.5.4: Position sync — detect naked positions immediately
            if not PAPER_TRADE and signer:
                sync_handled = await position_sync_check(signer, local_pos)
                if sync_handled:
                    await asyncio.sleep(3); continue
            locked = is_entry_locked()
            cd = max(0, int(last_trade_time + MIN_TRADE_INTERVAL - now))
            dir_cd = max(0, int(last_dir_close_time + SAME_DIR_COOLDOWN - now))
            hold = local_pos.hold_time()
            with tick_lock: n_ticks = len(tick_prices)
            mom_str = f"{momentum*100:.3f}%" if momentum is not None else "n/a"
            pos_str = f"{local_pos.side[0].upper()}" if local_pos.in_position else "—"
            oco_tag = "⚡" if local_pos.oco_attached else "👁" if local_pos.in_position else ""
            lk = "🔒" if locked else ""

            print(f"[{time.strftime('%H:%M:%S')}] ${price:,.2f} imb={ie:+.3f} mom={mom_str} pos={pos_str}{oco_tag} pnl=${daily_pnl:.2f} cd={cd}s hold={hold}s {lk}")

            # ══════════════════════════════════════════════════════════════
            # IN POSITION
            # ══════════════════════════════════════════════════════════════
            if local_pos.in_position:
                is_long = (local_pos.side == 'long')
                unrealized_pct = local_pos.unrealized_pct(price)

                # ── v6.5.1: HARD STOP BACKSTOP ──
                # If price has blown way past SL (>0.15%), force immediate close
                # This catches gaps/spikes where the OCO limit order didn't fill
                if is_long and price < local_pos.entry_price * (1 - HARD_STOP_PCT):
                    print(f"  🛑 HARD STOP! Price ${price:,.0f} is {unrealized_pct*100:.2f}% below entry (>{HARD_STOP_PCT*100:.2f}% limit)")
                    _side = local_pos.side; _entry = local_pos.entry_price
                    success = await close_position(signer, local_pos, "hard_stop", price)
                    if success:
                        fill = ws_get_last_fill()
                        exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                        pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                        daily_pnl += pnl_usd; daily_trades += 1
                        track_direction_pnl(_side, pnl_usd)
                        tg(f"🛑 HARD STOP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                        if pnl_usd >= 0: wins += 1
                        else: losses += 1
                        last_dir_close_time = now; last_trade_time = now; unlock_entry()
                        save_state({"last_trade_time": now, "last_dir_close_time": now,
                            "last_direction": last_direction, "daily_pnl": daily_pnl,
                            "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                    await asyncio.sleep(3); continue
                elif not is_long and price > local_pos.entry_price * (1 + HARD_STOP_PCT):
                    print(f"  🛑 HARD STOP! Price ${price:,.0f} is {abs(unrealized_pct)*100:.2f}% above entry (>{HARD_STOP_PCT*100:.2f}% limit)")
                    _side = local_pos.side; _entry = local_pos.entry_price
                    success = await close_position(signer, local_pos, "hard_stop", price)
                    if success:
                        fill = ws_get_last_fill()
                        exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                        pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                        daily_pnl += pnl_usd; daily_trades += 1
                        track_direction_pnl(_side, pnl_usd)
                        tg(f"🛑 HARD STOP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                        if pnl_usd >= 0: wins += 1
                        else: losses += 1
                        last_dir_close_time = now; last_trade_time = now; unlock_entry()
                        save_state({"last_trade_time": now, "last_dir_close_time": now,
                            "last_direction": last_direction, "daily_pnl": daily_pnl,
                            "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                    await asyncio.sleep(3); continue

                # ── v6.5.3: TRAILING STOP CHECK ──
                trail_result = local_pos.update_trailing_stop(price)
                if trail_result == 'trail_exit':
                    best_pct = abs(local_pos.best_price - local_pos.entry_price) / local_pos.entry_price * 100
                    print(f"  🔄 TRAIL EXIT! Best: ${local_pos.best_price:,.1f} (+{best_pct:.3f}%) → stop triggered at ${price:,.1f}")
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct, fill = paper.execute_exit(local_pos.side, local_pos.entry_price, price, "trail_exit")
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        tg(f"🔄 TRAIL EXIT ${fill:,.0f}\nBest: ${local_pos.best_price:,.0f}\n{pnl_pct*100:+.3f}% (${pnl_usd:+.3f})\n{paper.summary()}")
                        local_pos.close("trail_exit"); last_trade_time = now; last_dir_close_time = now; unlock_entry()
                        save_state({"last_trade_time": now, "last_dir_close_time": now,
                            "last_direction": last_direction, "daily_pnl": daily_pnl,
                            "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                        await asyncio.sleep(3); continue
                    else:
                        _side = local_pos.side; _entry = local_pos.entry_price
                        _best = local_pos.best_price
                        success = await close_position(signer, local_pos, "trail_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            tg(f"🔄 TRAIL EXIT ${exit_p:,.0f}\nBest: ${_best:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1
                            else: losses += 1
                            last_dir_close_time = now; last_trade_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                        await asyncio.sleep(3); continue

                # ── TP/SL CHECK ──
                # v6.5.3: Skip TP if trailing is active (let it run)
                tp_sl = local_pos.check_tp_sl(price)
                if tp_sl == 'tp' and local_pos.trailing_active:
                    tp_sl = None  # trailing stop handles exit instead
                if tp_sl:
                    if PAPER_TRADE:
                        exit_p = local_pos.tp_price if tp_sl == 'tp' else local_pos.sl_price
                        pnl_usd, pnl_pct, fill = paper.execute_exit(local_pos.side, local_pos.entry_price, exit_p, tp_sl)
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        emoji = "✅" if tp_sl == 'tp' else "❌"
                        tg(f"{emoji} {'TP' if tp_sl=='tp' else 'SL'} ${fill:,.0f}\n{pnl_pct*100:+.3f}% (${pnl_usd:+.3f})\n{paper.summary()}")
                        local_pos.close(tp_sl); last_trade_time = now; last_dir_close_time = now; unlock_entry()
                        save_state({"last_trade_time": now, "last_dir_close_time": now,
                            "last_direction": last_direction, "daily_pnl": daily_pnl,
                            "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                        await asyncio.sleep(3); continue

                    elif local_pos.oco_attached and tp_sl == 'sl':
                        # v6.5.7: SL crossed — immediately market close, don't wait for exchange SL
                        # Exchange SL has slippage in fast moves. Bot market-close is faster.
                        if ws_is_flat():
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if fill else local_pos.sl_price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(local_pos.side, local_pos.entry_price, exit_p)
                            print(f"  ✅ Exchange SL already filled @ ${exit_p:,.1f}")
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(local_pos.side, pnl_usd)
                            emoji = "❌"
                            tg(f"{emoji} SL ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1
                            else: losses += 1
                            await cancel_stale_orders(signer)
                            local_pos.close("sl")
                            last_dir_close_time = now; last_trade_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue
                        else:
                            print(f"  🚨 SL crossed — immediate market close (skipping exchange SL)")
                            _side = local_pos.side; _entry = local_pos.entry_price
                            success = await close_position(signer, local_pos, "fast_sl", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                                daily_pnl += pnl_usd; daily_trades += 1
                                track_direction_pnl(_side, pnl_usd)
                                emoji = "❌"
                                tg(f"{emoji} FAST SL ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                                if pnl_usd >= 0: wins += 1
                                else: losses += 1
                                last_dir_close_time = now; last_trade_time = now; unlock_entry()
                                save_state({"last_trade_time": now, "last_dir_close_time": now,
                                    "last_direction": last_direction, "daily_pnl": daily_pnl,
                                    "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue

                    elif local_pos.oco_attached and tp_sl == 'tp':
                        # TP crossed — check if exchange already handled it
                        print(f"  🔍 TP crossed, checking exchange...")
                        if ws_is_flat():
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if fill else local_pos.tp_price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(local_pos.side, local_pos.entry_price, exit_p)
                            print(f"  ✅ TP confirmed via WS. Fill: ${exit_p:,.1f}")
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(local_pos.side, pnl_usd)
                            emoji = "✅"
                            tg(f"{emoji} TP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1
                            else: losses += 1
                            await cancel_stale_orders(signer)
                            local_pos.close("tp")
                            last_dir_close_time = now; last_trade_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue
                        else:
                            # TP not filled yet — force close to lock in profit
                            print(f"  📈 TP crossed, force closing to lock profit")
                            _side = local_pos.side; _entry = local_pos.entry_price
                            success = await close_position(signer, local_pos, "fast_tp", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                                daily_pnl += pnl_usd; daily_trades += 1
                                track_direction_pnl(_side, pnl_usd)
                                emoji = "✅" if pnl_usd >= 0 else "❌"
                                tg(f"{emoji} TP ${exit_p:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                                if pnl_usd >= 0: wins += 1
                                else: losses += 1
                                last_dir_close_time = now; last_trade_time = now; unlock_entry()
                                save_state({"last_trade_time": now, "last_dir_close_time": now,
                                    "last_direction": last_direction, "daily_pnl": daily_pnl,
                                    "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue

                    else:
                        print(f"  🚨 {'TP' if tp_sl=='tp' else 'SL'} hit (no OCO), force closing!")
                        _side = local_pos.side; _entry = local_pos.entry_price
                        success = await close_position(signer, local_pos, f"local_{tp_sl}", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            emoji = "✅" if pnl_usd >= 0 else "❌"
                            tg(f"{emoji} {'TP' if tp_sl=='tp' else 'SL'} ${price:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1
                            else: losses += 1
                            last_dir_close_time = now; last_trade_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                        await asyncio.sleep(3); continue

                # ── IMBALANCE EXIT (backup, only if profitable) ──
                # v6.5.3: Skip imb_exit when trailing — let the trail ride
                if unrealized_pct > IMB_EXIT_MIN_PROFIT and not local_pos.trailing_active:
                    imb_against = (is_long and ie < -IMB_EXIT_THRESHOLD) or (not is_long and ie > IMB_EXIT_THRESHOLD)
                    if imb_against:
                        if PAPER_TRADE:
                            pnl_usd, pnl_pct, fill = paper.execute_exit(local_pos.side, local_pos.entry_price, price, "imb_exit")
                            daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                            tg(f"📈 IMB ${fill:,.0f}\n{pnl_pct*100:+.3f}% (${pnl_usd:+.3f})\n{paper.summary()}")
                            local_pos.close("imb_exit"); last_trade_time = now; last_dir_close_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue
                        else:
                            print(f"  📈 Imb exit +{unrealized_pct*100:.3f}%")
                            _side = local_pos.side; _entry = local_pos.entry_price
                            success = await close_position(signer, local_pos, "imb_exit", price)
                            if success:
                                fill = ws_get_last_fill()
                                exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                                pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                                daily_pnl += pnl_usd; daily_trades += 1
                                track_direction_pnl(_side, pnl_usd)
                                emoji = "✅" if pnl_usd >= 0 else "❌"
                                tg(f"📈 {emoji} ${price:,.0f}\n${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                                if pnl_usd >= 0: wins += 1
                                else: losses += 1
                                last_dir_close_time = now; last_trade_time = now; unlock_entry()
                                save_state({"last_trade_time": now, "last_dir_close_time": now,
                                    "last_direction": last_direction, "daily_pnl": daily_pnl,
                                    "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue

                # ── TIME EXIT ──
                # v6.5.3: Extended hold time when trailing stop is active
                effective_max_hold = TRAIL_MAX_HOLD_SECS if local_pos.trailing_active else MAX_HOLD_SECS
                if hold > effective_max_hold:
                    if PAPER_TRADE:
                        pnl_usd, pnl_pct, fill = paper.execute_exit(local_pos.side, local_pos.entry_price, price, "time_exit")
                        daily_pnl = paper.daily_pnl; wins = paper.wins; losses = paper.losses; daily_trades = paper.total_trades
                        emoji = "✅" if pnl_usd >= 0 else "❌"
                        tg(f"⏱ {emoji} {hold}s ${pnl_usd:+.3f}\n{paper.summary()}")
                        local_pos.close("time_exit"); last_trade_time = now; last_dir_close_time = now; unlock_entry()
                        save_state({"last_trade_time": now, "last_dir_close_time": now,
                            "last_direction": last_direction, "daily_pnl": daily_pnl,
                            "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                        await asyncio.sleep(3); continue
                    else:
                        print(f"  ⏱ Time exit {hold}s")
                        _side = local_pos.side; _entry = local_pos.entry_price
                        success = await close_position(signer, local_pos, "time_exit", price)
                        if success:
                            fill = ws_get_last_fill()
                            exit_p = fill["price"] if (fill and time.time() - fill["time"] < 5) else price
                            pnl_usd, pnl_pct = calc_pnl_from_prices(_side, _entry, exit_p)
                            daily_pnl += pnl_usd; daily_trades += 1
                            track_direction_pnl(_side, pnl_usd)
                            emoji = "✅" if pnl_usd >= 0 else "❌"
                            tg(f"⏱ {emoji} {hold}s ${pnl_usd:+.3f} ({pnl_pct*100:+.3f}%) | Day: ${daily_pnl:.2f}")
                            if pnl_usd >= 0: wins += 1
                            else: losses += 1
                            last_dir_close_time = now; last_trade_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                        await asyncio.sleep(3); continue
                else:
                    oco_tag = "⚡" if local_pos.oco_attached else "👁"
                    tbias = tape_get_recent_bias(3)
                    tape_str = f" tape:{tbias:+.2f}" if abs(tbias) > 0.3 else ""
                    trail_str = f" 🔄trail stop=${local_pos.trail_stop_price:,.1f}" if local_pos.trailing_active else ""
                    print(f"  Holding {local_pos.side} ${local_pos.entry_price:,.0f} pnl={unrealized_pct*100:.3f}% {hold}s {oco_tag}{tape_str}{trail_str}")

            # ══════════════════════════════════════════════════════════════
            # ENTRY LOGIC
            # ══════════════════════════════════════════════════════════════
            elif not local_pos.in_position and not locked:
                long_blocked = last_direction == 'long' and now - last_dir_close_time < SAME_DIR_COOLDOWN
                short_blocked = last_direction == 'short' and now - last_dir_close_time < SAME_DIR_COOLDOWN

                if n_ticks < warmup_ticks: print("  Warming up...")
                elif paused: print("  Paused")
                elif daily_pnl <= -DAILY_LOSS_LIMIT: print("  Daily loss limit")
                elif trades_this_hour >= MAX_TRADES_HOUR: print("  Max trades/hr")
                elif cd > 0: print(f"  CD {cd}s")
                elif not volatility_ok(): pass
                elif not chop_filter_ok(): pass

                # ── LONG ──
                elif ie > IMB_ENTRY_THRESHOLD and not long_blocked:
                    vamp_ok = vamp_d > VAMP_MIN_DIVERGENCE
                    mom_ok = momentum is None or momentum > MOM_BLOCK_THRESHOLD
                    pct_ok = percentile_allows('long')
                    trend_ok = trend_filter('long')
                    if not pct_ok: pass
                    elif not trend_ok: pass
                    elif not vamp_ok: print(f"  🟡 LONG: VAMP ({vamp_d*100:+.4f}%)")
                    elif not mom_ok: print(f"  🟡 LONG: mom ({momentum*100:.3f}%)")
                    else:
                        if PAPER_TRADE:
                            fill = paper.execute_entry('long', price)
                            lock_entry(); local_pos.open('long', fill)
                            last_trade_time = now; last_direction = 'long'; trades_this_hour += 1
                            tg(f"🟢 LONG ${fill:,.0f}\nTP:${local_pos.tp_price:,.0f} SL:${local_pos.sl_price:,.0f}\nImb:{ie:+.3f}")
                            save_state({"last_trade_time": now, "last_dir_close_time": last_dir_close_time,
                                "last_direction": 'long', "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(5); continue
                        else:
                            # v6.5: Check WS for existing position instead of REST API
                            if not ws_is_flat():
                                print(f"  ⛔ Entry blocked: WS shows position open")
                                lock_entry()
                                await asyncio.sleep(10); continue

                            lock_entry()
                            # v6.5.1: Log momentum and tape for analysis
                            if momentum and abs(momentum) > 0.001:
                                print(f"  📊 Entry mom: {momentum*100:+.3f}% | tape bias: {tape_get_recent_bias(3):+.2f}")
                            print(f"  🟢 LONG ${price:,.0f} imb={ie:+.3f}")
                            await cancel_stale_orders(signer)
                            _, _, err = await signer.create_market_order_limited_slippage(
                                market_index=MARKET_INDEX, client_order_index=0,
                                base_amount=BASE_AMOUNT, max_slippage=0.005, is_ask=0)
                            if err:
                                tg(f"❌ Order failed: {err}"); unlock_entry()
                            else:
                                local_pos.open('long', price)
                                last_trade_time = now; last_direction = 'long'; trades_this_hour += 1
                                tg(f"🟢 LONG ${price:,.0f}\nTP:${local_pos.tp_price:,.0f} SL:${local_pos.sl_price:,.0f}\nImb:{ie:+.3f}")
                                save_state({"last_trade_time": now, "last_dir_close_time": last_dir_close_time,
                                    "last_direction": 'long', "daily_pnl": daily_pnl,
                                    "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                                asyncio.create_task(attach_oco_background(signer, True, price))
                                await asyncio.sleep(10); continue

                # ── SHORT ──
                elif ie < -IMB_ENTRY_THRESHOLD and not short_blocked:
                    vamp_ok = vamp_d < -VAMP_MIN_DIVERGENCE
                    mom_ok = momentum is None or momentum < -MOM_BLOCK_THRESHOLD
                    pct_ok = percentile_allows('short')
                    trend_ok = trend_filter('short')
                    if not pct_ok: pass
                    elif not trend_ok: pass
                    elif not vamp_ok: print(f"  🟡 SHORT: VAMP ({vamp_d*100:+.4f}%)")
                    elif not mom_ok: print(f"  🟡 SHORT: mom ({momentum*100:.3f}%)")
                    else:
                        if PAPER_TRADE:
                            fill = paper.execute_entry('short', price)
                            lock_entry(); local_pos.open('short', fill)
                            last_trade_time = now; last_direction = 'short'; trades_this_hour += 1
                            tg(f"🔴 SHORT ${fill:,.0f}\nTP:${local_pos.tp_price:,.0f} SL:${local_pos.sl_price:,.0f}\nImb:{ie:+.3f}")
                            save_state({"last_trade_time": now, "last_dir_close_time": last_dir_close_time,
                                "last_direction": 'short', "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(5); continue
                        else:
                            # v6.5: Check WS for existing position instead of REST API
                            if not ws_is_flat():
                                print(f"  ⛔ Entry blocked: WS shows position open")
                                lock_entry()
                                await asyncio.sleep(10); continue

                            lock_entry()
                            # v6.5.1: Log momentum and tape for analysis
                            if momentum and abs(momentum) > 0.001:
                                print(f"  📊 Entry mom: {momentum*100:+.3f}% | tape bias: {tape_get_recent_bias(3):+.2f}")
                            print(f"  🔴 SHORT ${price:,.0f} imb={ie:+.3f}")
                            await cancel_stale_orders(signer)
                            _, _, err = await signer.create_market_order_limited_slippage(
                                market_index=MARKET_INDEX, client_order_index=0,
                                base_amount=BASE_AMOUNT, max_slippage=0.005, is_ask=1)
                            if err:
                                tg(f"❌ Order failed: {err}"); unlock_entry()
                            else:
                                local_pos.open('short', price)
                                last_trade_time = now; last_direction = 'short'; trades_this_hour += 1
                                tg(f"🔴 SHORT ${price:,.0f}\nTP:${local_pos.tp_price:,.0f} SL:${local_pos.sl_price:,.0f}\nImb:{ie:+.3f}")
                                save_state({"last_trade_time": now, "last_dir_close_time": last_dir_close_time,
                                    "last_direction": 'short', "daily_pnl": daily_pnl,
                                    "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                                asyncio.create_task(attach_oco_background(signer, False, price))
                                await asyncio.sleep(10); continue
                else:
                    blocked_msg = f" (dir:{dir_cd}s)" if (long_blocked or short_blocked) else ""
                    print(f"  Waiting imb={ie:+.3f} (need ±{IMB_ENTRY_THRESHOLD}){blocked_msg}")

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
