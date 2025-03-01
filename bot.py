#!/usr/bin/env python3
"""
Scalp Bot v6.4.4 — STARTUP POSITION SAFETY
  Only change from v6.4.3:
  - On startup: query API for open positions, AUTO-CLOSE them before trading
  - No more "close manually" pause — bot handles it
  - PnL tracking resets fresh each restart (fixes ghost PnL bug)
  Everything else identical to v6.4.3.
"""
import asyncio, time, requests, lighter, threading, json, collections, os, traceback
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

BASE_AMOUNT=500  # 0.005 BTC (~$335 notional)

# ── STRATEGY PARAMS ─────────────────────────────────────────────────────
TP_PCT=0.0005        # 0.05% TP
SL_PCT=0.0008        # 0.08% SL — R:R 0.63:1, needs 58%+ WR

MAX_HOLD_SECS=60     # reduced from 90 — cut losers faster
MIN_TRADE_INTERVAL=15
MAX_TRADES_HOUR=60
DAILY_LOSS_LIMIT=2.0
SAME_DIR_COOLDOWN=15

# ── SIGNALS ─────────────────────────────────────────────────────────────
IMB_LEVELS=5; IMB_EMA_ALPHA=0.1
IMB_ENTRY_THRESHOLD=0.30
IMB_EXIT_THRESHOLD=0.15
IMB_EXIT_MIN_PROFIT=0.0003  # 0.03%
VAMP_MIN_DIVERGENCE=0.0
SMOOTH_TICKS=10; LOOKBACK_TICKS=30; MOM_BLOCK_THRESHOLD=0.0
PERCENTILE_LOOKBACK=300; PERCENTILE_UPPER=92; PERCENTILE_LOWER=8

# ── VOLATILITY FILTER ───────────────────────────────────────────────────
VOL_LOOKBACK_SECS=300
VOL_MIN_RANGE_PCT=0.10

# ── SIMPLIFIED EXIT ─────────────────────────────────────────────────────
EXIT_SETTLE_SECS=5   # seconds to wait after exit order before clearing state
LOCK_DURATION=90     # covers max_hold(60) + settle(5) + buffer

STATE_FILE="/root/rsi_bot/.bot_state.json"
LOCK_FILE="/root/rsi_bot/.entry_lock"
LOCAL_POS_FILE="/root/rsi_bot/.local_position.json"

tick_prices=collections.deque(maxlen=800); tick_lock=threading.Lock()
ob_bids=[]; ob_asks=[]; ob_lock=threading.Lock()
imb_ema=0.0; imb_ema_lock=threading.Lock()
momentum_history=collections.deque(maxlen=800)


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
    def on_msg(ws, msg):
        global ob_bids, ob_asks, imb_ema
        try:
            ob = json.loads(msg).get("order_book", {})
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
        except: pass
    def on_close(ws, *a): time.sleep(2); run()
    def on_open(ws):
        print("  WS connected")
        ws.send(json.dumps({"type": "subscribe", "channel": "order_book/1"}))
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
# ══════════════════════════════════════════════════════════════════════════
async def attach_oco(signer, entry, is_long):
    try:
        if is_long:
            tp_p = entry * (1 + TP_PCT); sl_p = entry * (1 - SL_PCT); ea = 1
            tp_l = int(tp_p * 1.001 * 10); sl_l = int(sl_p * 0.999 * 10)
        else:
            tp_p = entry * (1 - TP_PCT); sl_p = entry * (1 + SL_PCT); ea = 0
            tp_l = int(tp_p * 0.999 * 10); sl_l = int(sl_p * 1.001 * 10)
        tp_t = int(tp_p * 10); sl_t = int(sl_p * 10)
        tp_o = CreateOrderTxReq(MarketIndex=MARKET_INDEX, ClientOrderIndex=0, BaseAmount=0, Price=tp_l, IsAsk=ea,
            Type=signer.ORDER_TYPE_TAKE_PROFIT_LIMIT, TimeInForce=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1, TriggerPrice=tp_t, OrderExpiry=-1)
        sl_o = CreateOrderTxReq(MarketIndex=MARKET_INDEX, ClientOrderIndex=0, BaseAmount=0, Price=sl_l, IsAsk=ea,
            Type=signer.ORDER_TYPE_STOP_LOSS_LIMIT, TimeInForce=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1, TriggerPrice=sl_t, OrderExpiry=-1)
        _, _, err = await signer.create_grouped_orders(grouping_type=signer.GROUPING_TYPE_ONE_CANCELS_THE_OTHER, orders=[tp_o, sl_o])
        return err, sl_p, tp_p
    except Exception as e: return str(e), 0, 0

async def attach_oco_background(signer, is_long, entry_price):
    """Try to attach OCO. If it fails, mark oco_attached=False so bot monitors locally."""
    await asyncio.sleep(2)
    for attempt in range(3):
        try:
            err, sl_p, tp_p = await attach_oco(signer, entry_price, is_long)
            if not err:
                local_pos.oco_attached = True; local_pos._save()
                tg(f"✅ OCO TP:${tp_p:,.0f} SL:${sl_p:,.0f}")
                return
            print(f"  OCO attempt {attempt+1}: {err}")
        except Exception as e:
            print(f"  OCO attempt {attempt+1}: {e}")
        await asyncio.sleep(2)
    # OCO FAILED — bot will monitor TP/SL locally
    local_pos.oco_attached = False; local_pos._save()
    tg("⚠️ OCO failed — bot monitoring TP/SL locally")


# ══════════════════════════════════════════════════════════════════════════
# SIMPLIFIED EXIT — trust the order, not the API
# ══════════════════════════════════════════════════════════════════════════
async def send_market_close(signer, is_long):
    """Send market close order. Returns True if order accepted (no error)."""
    try:
        # Cancel any existing orders first
        ts = int(time.time() * 1000)
        await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=ts)
        await asyncio.sleep(1)
    except Exception as e:
        print(f"  ⚠️ Cancel error: {e}")

    _, _, err = await signer.create_market_order_limited_slippage(
        market_index=MARKET_INDEX, client_order_index=0,
        base_amount=BASE_AMOUNT, max_slippage=0.005,
        is_ask=1 if is_long else 0)

    if err:
        print(f"  ❌ Exit order failed: {err}")
        tg(f"⚠️ Exit failed: {err}")
        return False
    return True

async def close_position(signer, local_pos, reason, price):
    """
    v6.4.3: Simple exit flow.
    Send order → no error → wait 5s → clear state. No API polling.
    """
    is_long = (local_pos.side == 'long')
    entry = local_pos.entry_price

    if PAPER_TRADE:
        return True

    print(f"  🔄 Closing: {reason}")
    success = await send_market_close(signer, is_long)

    if success:
        # Trust the order — wait for settlement, then clear
        print(f"  ✅ Exit order accepted, settling {EXIT_SETTLE_SECS}s...")
        await asyncio.sleep(EXIT_SETTLE_SECS)
        local_pos.close(reason)
        return True
    else:
        # Order failed — try once more
        print(f"  🔄 Retry exit...")
        await asyncio.sleep(2)
        success2 = await send_market_close(signer, is_long)
        if success2:
            await asyncio.sleep(EXIT_SETTLE_SECS)
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
                ts = int(time.time() * 1000)
                await signer.cancel_all_orders(time_in_force=signer.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=ts)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"  ⚠️ Cancel error: {e}")
        
        if pos["size"] > 0:
            is_long = (pos["sign"] > 0)
            side_str = "LONG" if is_long else "SHORT"
            print(f"  🚨 Orphaned {side_str} position: {pos['size']:.5f} BTC @ ${pos['entry']:,.1f}")
            print(f"  🔄 Auto-closing orphaned position...")
            tg(f"🚨 Orphaned {side_str} found on startup\nSize: {pos['size']:.5f} @ ${pos['entry']:,.1f}\n🔄 Auto-closing...")
            
            # Send market close
            _, _, err = await signer.create_market_order_limited_slippage(
                market_index=MARKET_INDEX, client_order_index=0,
                base_amount=int(pos["size"] * 100000),  # convert to base amount
                max_slippage=0.01,  # wider slippage for safety
                is_ask=1 if is_long else 0)
            
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
last_update_id = 0; paused = False; last_day = state["last_day"]
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
                tg(f"📊 v6.4.4 [{mode}]\n${price:,.0f} | Imb:{ie:+.2f}\n"
                   f"Pos: {pos_str} {oco_str}\n"
                   f"Day: ${daily_pnl:.2f} | {wins}W/{losses}L ({wr})\n"
                   f"TP:0.05% SL:0.08% Hold:{MAX_HOLD_SECS}s")
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
    print(f"Scalp Bot v6.4.4 [{mode_str}]")
    print(f"  TP:{TP_PCT*100:.2f}% SL:{SL_PCT*100:.2f}% R:R={TP_PCT/SL_PCT:.2f}:1")
    print(f"  Hold:{MAX_HOLD_SECS}s CD:{MIN_TRADE_INTERVAL}s DirCD:{SAME_DIR_COOLDOWN}s")
    print(f"  Vol filter: {VOL_MIN_RANGE_PCT}% 5min | Exit: trust order + {EXIT_SETTLE_SECS}s settle")
    print(f"  v6.4.4: Auto-close orphaned positions on startup")

    threading.Thread(target=ws_thread, daemon=True).start()
    await asyncio.sleep(3)

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

    prev_balance = None
    if not PAPER_TRADE:
        # Auto-close any orphaned positions on the exchange
        starting_balance = await startup_close_orphan(signer)
        if starting_balance is None:
            print("  🚨 ABORTING: Could not ensure clean state on startup")
            tg("🚨 Bot aborted — could not close orphaned position. Fix manually.")
            return
        prev_balance = starting_balance
    
    tg(f"🚀 v6.4.4 [{mode_str}]\nTP:{TP_PCT*100:.2f}% SL:{SL_PCT*100:.2f}% R:R {TP_PCT/SL_PCT:.2f}:1\n"
       f"Hold:{MAX_HOLD_SECS}s | VolFilter:{VOL_MIN_RANGE_PCT}%\n"
       f"Exit: trust order + {EXIT_SETTLE_SECS}s settle\n"
       f"🆕 Auto-close orphans on startup"
       + (f"\nPaper: ${paper.balance:.2f}" if PAPER_TRADE else f"\nBalance: ${prev_balance:.2f}"))

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

                # ── TP/SL CHECK ──
                tp_sl = local_pos.check_tp_sl(price)
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

                    elif local_pos.oco_attached:
                        print(f"  🔍 {'TP' if tp_sl=='tp' else 'SL'} crossed, OCO should handle...")
                        pos = api_get_position()
                        if pos and pos["size"] == 0 and pos["open_orders"] == 0:
                            if prev_balance is not None:
                                delta = pos["balance"] - prev_balance
                                daily_pnl += delta; daily_trades += 1
                                emoji = "✅" if delta >= 0 else "❌"
                                tg(f"{emoji} OCO {'TP' if tp_sl=='tp' else 'SL'} ${price:,.0f}\n${delta:+.3f} | Day: ${daily_pnl:.2f}")
                                if delta >= 0: wins += 1
                                else: losses += 1
                                prev_balance = pos["balance"]
                            local_pos.close(f"oco_{tp_sl}")
                            last_dir_close_time = now; last_trade_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue
                        else:
                            print(f"  🚨 OCO didn't fire, force closing!")
                            success = await close_position(signer, local_pos, f"forced_{tp_sl}", price)
                            if success:
                                pos2 = api_get_position()
                                if pos2 and prev_balance is not None:
                                    delta = pos2["balance"] - prev_balance
                                    daily_pnl += delta; daily_trades += 1
                                    emoji = "✅" if delta >= 0 else "❌"
                                    tg(f"{emoji} FORCED {'TP' if tp_sl=='tp' else 'SL'} ${price:,.0f}\n${delta:+.3f} | Day: ${daily_pnl:.2f}")
                                    if delta >= 0: wins += 1
                                    else: losses += 1
                                    prev_balance = pos2["balance"]
                                last_dir_close_time = now; last_trade_time = now; unlock_entry()
                                save_state({"last_trade_time": now, "last_dir_close_time": now,
                                    "last_direction": last_direction, "daily_pnl": daily_pnl,
                                    "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue

                    else:
                        print(f"  🚨 {'TP' if tp_sl=='tp' else 'SL'} hit (no OCO), force closing!")
                        success = await close_position(signer, local_pos, f"local_{tp_sl}", price)
                        if success:
                            pos2 = api_get_position()
                            if pos2 and prev_balance is not None:
                                delta = pos2["balance"] - prev_balance
                                daily_pnl += delta; daily_trades += 1
                                emoji = "✅" if delta >= 0 else "❌"
                                tg(f"{emoji} {'TP' if tp_sl=='tp' else 'SL'} ${price:,.0f}\n${delta:+.3f} | Day: ${daily_pnl:.2f}")
                                if delta >= 0: wins += 1
                                else: losses += 1
                                prev_balance = pos2["balance"]
                            last_dir_close_time = now; last_trade_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                        await asyncio.sleep(3); continue

                # ── IMBALANCE EXIT (backup, only if profitable) ──
                if unrealized_pct > IMB_EXIT_MIN_PROFIT:
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
                            success = await close_position(signer, local_pos, "imb_exit", price)
                            if success:
                                pos2 = api_get_position()
                                if pos2 and prev_balance is not None:
                                    delta = pos2["balance"] - prev_balance
                                    daily_pnl += delta; daily_trades += 1
                                    emoji = "✅" if delta >= 0 else "❌"
                                    tg(f"📈 {emoji} ${price:,.0f}\n${delta:+.3f} | Day: ${daily_pnl:.2f}")
                                    if delta >= 0: wins += 1
                                    else: losses += 1
                                    prev_balance = pos2["balance"]
                                last_dir_close_time = now; last_trade_time = now; unlock_entry()
                                save_state({"last_trade_time": now, "last_dir_close_time": now,
                                    "last_direction": last_direction, "daily_pnl": daily_pnl,
                                    "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                            await asyncio.sleep(3); continue

                # ── TIME EXIT ──
                if hold > MAX_HOLD_SECS:
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
                        success = await close_position(signer, local_pos, "time_exit", price)
                        if success:
                            pos2 = api_get_position()
                            if pos2 and prev_balance is not None:
                                delta = pos2["balance"] - prev_balance
                                daily_pnl += delta; daily_trades += 1
                                emoji = "✅" if delta >= 0 else "❌"
                                tg(f"⏱ {emoji} {hold}s ${delta:+.3f} | Day: ${daily_pnl:.2f}")
                                if delta >= 0: wins += 1
                                else: losses += 1
                                prev_balance = pos2["balance"]
                            last_dir_close_time = now; last_trade_time = now; unlock_entry()
                            save_state({"last_trade_time": now, "last_dir_close_time": now,
                                "last_direction": last_direction, "daily_pnl": daily_pnl,
                                "wins": wins, "losses": losses, "daily_trades": daily_trades, "last_day": last_day})
                        await asyncio.sleep(3); continue
                else:
                    oco_tag = "⚡" if local_pos.oco_attached else "👁"
                    print(f"  Holding {local_pos.side} ${local_pos.entry_price:,.0f} pnl={unrealized_pct*100:.3f}% {hold}s {oco_tag}")

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

                # ── LONG ──
                elif ie > IMB_ENTRY_THRESHOLD and not long_blocked:
                    vamp_ok = vamp_d > VAMP_MIN_DIVERGENCE
                    mom_ok = momentum is None or momentum > MOM_BLOCK_THRESHOLD
                    pct_ok = percentile_allows('long')
                    if not pct_ok: pass
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
                            pos = api_get_position()
                            if pos and pos["size"] > 0:
                                print(f"  ⛔ Entry blocked: position exists on API")
                                lock_entry()
                                await asyncio.sleep(10); continue

                            lock_entry()
                            if pos: prev_balance = pos["balance"]
                            print(f"  🟢 LONG ${price:,.0f} imb={ie:+.3f}")
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
                    if not pct_ok: pass
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
                            pos = api_get_position()
                            if pos and pos["size"] > 0:
                                print(f"  ⛔ Entry blocked: position exists on API")
                                lock_entry()
                                await asyncio.sleep(10); continue

                            lock_entry()
                            if pos: prev_balance = pos["balance"]
                            print(f"  🔴 SHORT ${price:,.0f} imb={ie:+.3f}")
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
