"""Delta Exchange trader for the 15-minute E strategy (BTC, ETH, gold).

Rule (long; shorts are the mirror image)
  Entry   market order at the close of a 15M E bar (RSI crossed above 50 after an extreme low)
  Stop    top of the last wick: the lowest low since the RSI extreme (hard stop on the exchange, hit on touch)
  Target  the moving EMA200 minus TARGET_ATR x ATR ("just before 200 EMA"); moved every bar, closed at market when reached
  If the stop is hit: a contra trade in the opposite direction, stop = the extreme the failed trade reached since the E bar, target = CONTRA_R x risk
  Limits  max hold 50 h (main) / 100 h (contra)

SAFETY (all on by default)
  * DELTA_DRY_RUN=1 (default): signals and order plans are printed / sent to Telegram, nothing is sent to Delta.
  * DELTA_ENV=testnet (default). "live" is refused unless DELTA_LIVE_CONFIRM=YES-I-AM-TRADING-REAL-MONEY.
  * Hard limits: risk per trade, max leverage (stop must be well inside the liquidation distance), max open trades,
    daily loss limit, symbol allow-list, and a kill file named STOP (new entries stop while it exists).
  * The API key and secret come only from the environment (DELTA_API_KEY / DELTA_API_SECRET). Never put them in the repo.
  * Delta requires whitelisted IPs for trading keys, so live use needs a host with a fixed IP (not GitHub Actions).

Usage
    python delta_bot.py --check          read-only: product info, live prices, wallet balance (needs keys), settings
    python delta_bot.py --once           one pass: manage open trades, then look for a new E (run every minute from cron)
    python delta_bot.py --status         print the trades in the state file
    python delta_bot.py --test-order     TESTNET ONLY: opens and closes 1 BTCUSD contract to prove the API calls work
    python delta_bot.py --live-test      LIVE: one real BTCUSD contract (about 65 USD of exposure), opened and closed at once.
                                         Needs DELTA_ENV=live, DELTA_DRY_RUN=0 and the confirmation variable.
"""
import argparse
import csv
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import rsi_tracker_scanner as S

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.environ.get("DELTA_ENV", "testnet").lower()
BASES = {"testnet": os.environ.get("DELTA_TESTNET_BASE", "https://cdn-ind.testnet.deltaex.org"),
         "live": os.environ.get("DELTA_LIVE_BASE", "https://api.india.delta.exchange")}
BASE = BASES[ENV]
PUBLIC = BASES["live"]                      # market data for the signals (public, no key)
DRY_RUN = os.environ.get("DELTA_DRY_RUN", "1") != "0"
LIVE_OK = os.environ.get("DELTA_LIVE_CONFIRM", "") == "YES-I-AM-TRADING-REAL-MONEY"

SYMBOLS = [s.strip() for s in os.environ.get("DELTA_SYMBOLS", "BTCUSD,ETHUSD,XAUTUSD").split(",") if s.strip()]
RISK_USD = float(os.environ.get("DELTA_RISK_USD", "2"))             # money lost if the stop is hit (before fees/slippage)
MAX_LEVERAGE = int(os.environ.get("DELTA_MAX_LEVERAGE", "10"))
MAX_OPEN = int(os.environ.get("DELTA_MAX_OPEN", "2"))
DAILY_LOSS_USD = float(os.environ.get("DELTA_DAILY_LOSS_USD", str(3 * RISK_USD)))
TARGET_ATR = float(os.environ.get("DELTA_TARGET_ATR", "0.5"))      # exit this many ATR before EMA200
CONTRA_R = float(os.environ.get("DELTA_CONTRA_R", "2"))
CONTRA_ON = os.environ.get("DELTA_CONTRA", "1") == "1"
MIN_RR = float(os.environ.get("DELTA_MIN_RR", "0"))                # skip trades whose target is closer than this many R
MIN_GAP_PCT = float(os.environ.get("DELTA_MIN_GAP_PCT", "1.0"))          # EMA20 must be at least this far from EMA200 (% of price): enough room for momentum
REQUIRE_COLOR = os.environ.get("DELTA_REQUIRE_COLOR", "1") == "1"  # the E candle must close in the trade direction (green for a buy, red for a sell)
TRIGGER = os.environ.get("DELTA_STOP_TRIGGER", "last_traded_price")  # or mark_price
MAIN_MAX_BARS, CONTRA_MAX_BARS = 200, 400                          # 15M bars: 50 h and 100 h
E_LEVEL = 50.0
STATE_FILE = os.environ.get("DELTA_STATE", os.path.join(HERE, "delta_state.json"))
LOG_FILE = os.path.join(HERE, "delta_trades.csv")
KILL_FILE = os.path.join(HERE, "STOP")
LOG_TXT = os.path.join(HERE, "delta_bot.log")          # readable history: every signal, order and result, plus one status line per 15M bar
UA = "rsi-scanner-delta/1.0"


# ---------------------------------------------------------------- API
def _http(method, base, path, params=None, body=None, auth=False):
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    if auth:
        key, secret = os.environ.get("DELTA_API_KEY", ""), os.environ.get("DELTA_API_SECRET", "")
        if not key or not secret:
            raise RuntimeError("DELTA_API_KEY / DELTA_API_SECRET are not set")
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(), (method + ts + path + qs + payload).encode(), hashlib.sha256).hexdigest()
        headers.update({"api-key": key, "timestamp": ts, "signature": sig})
    req = urllib.request.Request(base + path + qs, data=payload.encode() if payload else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {e.read().decode('utf-8', 'replace')[:400]}")


def public(path, params=None):
    return _http("GET", PUBLIC, path, params)


def private(method, path, params=None, body=None):
    if method != "GET":
        if ENV == "live" and not LIVE_OK:
            raise RuntimeError("live trading is not confirmed (DELTA_LIVE_CONFIRM)")
        if DRY_RUN:
            raise RuntimeError("dry run: refusing to send " + method + " " + path)
    return _http(method, BASE, path, params, body, auth=True)


def log_text(line):
    try:
        with open(LOG_TXT, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC | {line}\n")
    except OSError:
        pass


def note(msg):
    print(msg, flush=True)
    log_text(msg.replace("\n", " / "))
    try:
        S.send_telegram(msg)
    except BaseException as e:                     # Telegram is optional here
        if isinstance(e, KeyboardInterrupt):
            raise


def log_row(**kw):
    new = not os.path.exists(LOG_FILE)
    fields = ["time_utc", "id", "kind", "symbol", "side", "size", "entry", "exit", "stop", "target", "result", "pnl_R", "note"]
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: kw.get(k, "") for k in fields} | {"time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")})


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": {}, "trades": {}}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)


# ---------------------------------------------------------------- market data and signals
def candles(symbol, bars=700):
    end = int(time.time())
    res = public("/v2/history/candles", dict(resolution="15m", symbol=symbol, start=end - bars * 900, end=end))
    df = pd.DataFrame(res.get("result", []))
    if df.empty:
        return None
    df = df.drop_duplicates("time").sort_values("time")
    df["start"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}).set_index("start", drop=False)
    df = df[["Open", "High", "Low", "Close", "start"]].astype({"Open": float, "High": float, "Low": float, "Close": float})
    return df[df["start"] + pd.Timedelta(minutes=15) <= pd.Timestamp.now(tz="UTC")]       # closed bars only


_prod_cache = {}


def product(symbol):
    """Product info from the venue we trade on: product ids differ between testnet and live."""
    if symbol not in _prod_cache:
        try:
            _prod_cache[symbol] = _http("GET", BASE, f"/v2/products/{symbol}")["result"]
        except Exception as e:
            if ENV == "live":
                raise
            print(f"{symbol}: not found on {ENV} ({str(e)[:80]}), using live product data for the dry run only")
            _prod_cache[symbol] = public(f"/v2/products/{symbol}")["result"]
            _prod_cache[symbol]["_live_copy"] = True
    return _prod_cache[symbol]


def venue_price(symbol):
    """Price on the venue we trade on (testnet prices can differ from the live market)."""
    try:
        t = _http("GET", BASE, f"/v2/tickers/{symbol}")["result"]
    except Exception:
        if ENV == "live":
            raise
        t = public(f"/v2/tickers/{symbol}")["result"]           # dry run on a testnet without this market
    q = t.get("quotes") or {}
    return dict(last=float(t.get("close") or t.get("mark_price")), mark=float(t.get("mark_price") or t.get("close")),
                bid=float(q.get("best_bid") or t.get("mark_price")), ask=float(q.get("best_ask") or t.get("mark_price")))


def fmt_price(symbol, x):
    tick = float(product(symbol)["tick_size"])
    dec = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{round(round(x / tick) * tick, dec):.{dec}f}"


def find_signals(symbol, df):
    """E events on the last two closed bars. Returns plans in percentages so they can be applied to the venue price."""
    d = S.indicators(df)
    n = len(df)
    out = []
    for side, dd in (("long", d), ("short", S.mirror(d))):
        for i, ev, lvl, info in S.track(dd, E_LEVEL)[0]:
            if ev != "E" or i < n - 2:
                continue
            plan = S.paper_plan(dd, i)
            if not plan:
                continue
            entry = abs(plan["entry"])
            level = dd["emas"][i] - TARGET_ATR * dd["atr"][i]
            if level <= plan["entry"]:
                continue                                   # target would already be reached
            stop_pct, tp_pct = (plan["entry"] - plan["stop"]) / entry, (level - plan["entry"]) / entry
            if tp_pct / stop_pct < MIN_RR:
                continue
            out.append(dict(side=side, bar=df["start"].iloc[i].isoformat(), stop_pct=stop_pct, tp_pct=tp_pct, late=(i < n - 1),
                            color_ok=bool(dd["c"][i] > dd["o"][i]),
                            gap_pct=float((dd["emas"][i] - dd["emaf"][i]) / abs(dd["c"][i]) * 100)))
    return out


def target_pct_now(df, side):
    """Where the target is now, as a percentage of the last close (direction: toward profit)."""
    d = S.indicators(df)
    dd = d if side == "long" else S.mirror(d)
    c = abs(float(dd["c"][-1]))
    return (float(dd["emas"][-1]) - TARGET_ATR * float(dd["atr"][-1]) - float(dd["c"][-1])) / c


# ---------------------------------------------------------------- orders
def position_size(pid):
    r = private("GET", "/v2/positions", dict(product_id=pid)).get("result") or {}
    return int(r.get("size") or 0)


def fills_since(pid, ts):
    res = private("GET", "/v2/fills", dict(product_ids=str(pid), start_time=int((ts - 5) * 1e6), page_size=50))
    return res.get("result") or []


def avg_price(fills, side):
    f = [x for x in fills if x.get("side") == side]
    q = sum(float(x["size"]) for x in f)
    return sum(float(x["price"]) * float(x["size"]) for x in f) / q if q else None


def sizing(symbol, price, stop_pct, risk_usd):
    """Contracts so that hitting the stop loses about risk_usd, and a leverage that keeps liquidation beyond the stop."""
    cv = float(product(symbol)["contract_value"])
    per_contract = price * stop_pct * cv
    size = int(risk_usd // per_contract)
    lev = min(MAX_LEVERAGE, int(0.5 / stop_pct))           # liquidation is roughly 1/lev away: keep the stop within half of it
    return size, lev


def open_trade(state, symbol, side, bar, stop_px, tp_px, price, stop_pct, kind, parent=None, risk_usd=None):
    if product(symbol).get("_live_copy") and not DRY_RUN:
        note(f"⛔ {symbol} is not listed on {ENV}: skipped")
        return None
    pid = product(symbol)["id"]
    size, lev = sizing(symbol, price, stop_pct, risk_usd or RISK_USD)
    tag = f"{kind} {side.upper()} {symbol}"
    if size < 1:
        note(f"⛔ {tag}: skipped, risk ${risk_usd or RISK_USD:g} is too small for one contract at this stop distance")
        return None
    if lev < 1:
        note(f"⛔ {tag}: skipped, the stop is too far away ({stop_pct * 100:.1f}%) for a safe leverage")
        return None
    tid = f"{symbol}|{side}|{bar}|{kind}"
    plan = (f"{tag} | size {size} contracts x{lev} | entry about {fmt_price(symbol, price)} | stop {fmt_price(symbol, stop_px)} "
            f"({stop_pct * 100:.2f}%) | target {fmt_price(symbol, tp_px)} | risk about ${risk_usd or RISK_USD:g}")
    if DRY_RUN:
        note(f"🧪 DELTA DRY RUN ({ENV}): would open {plan}")
        return None
    private("POST", f"/v2/products/{pid}/orders/leverage", body=dict(leverage=str(lev)))
    body = dict(product_id=pid, size=size, side="buy" if side == "long" else "sell", order_type="market_order",
                bracket_stop_loss_price=fmt_price(symbol, stop_px), bracket_take_profit_price=fmt_price(symbol, tp_px),
                bracket_stop_trigger_method=TRIGGER, client_order_id="e" + hashlib.sha1(tid.encode()).hexdigest()[:30])
    res = private("POST", "/v2/orders", body=body)["result"]
    t = dict(id=tid, kind=kind, symbol=symbol, side=side, pid=pid, size=size, order_id=res.get("id"), opened=time.time(),
             stop=float(fmt_price(symbol, stop_px)), tp=float(fmt_price(symbol, tp_px)), stop_pct=stop_pct, status="open",
             entry=None, parent=parent, bar=bar)
    state["trades"][tid] = t
    note(f"✅ DELTA {ENV.upper()} order placed: {plan}")
    log_row(id=tid, kind=kind, symbol=symbol, side=side, size=size, stop=t["stop"], target=t["tp"], result="opened")
    return t


def edit_target(sym, pid, new_tp):
    """Move the take-profit order of an open position. Once the entry order has filled, the bracket is two ordinary
    stop orders on the position, so the take-profit order is found among the open orders and edited directly."""
    orders = private("GET", "/v2/orders", dict(product_ids=str(pid), states="open,pending")).get("result") or []
    tps = [o for o in orders if o.get("stop_order_type") == "take_profit_order"]
    if not tps:
        raise RuntimeError("no open take-profit order found: " + json.dumps([(o.get("stop_order_type"), o.get("order_type")) for o in orders]))
    o = tps[0]
    body = dict(id=o["id"], product_id=pid, product_symbol=sym, size=int(o["size"]), stop_price=fmt_price(sym, new_tp))
    if o.get("limit_price"):
        body["limit_price"] = fmt_price(sym, new_tp)
    private("PUT", "/v2/orders", body=body)
    return o


def close_market(t, why):
    side = "sell" if t["side"] == "long" else "buy"
    private("POST", "/v2/orders", body=dict(product_id=t["pid"], size=t["size"], side=side, order_type="market_order", reduce_only="true"))
    note(f"🔔 DELTA {t['kind']} {t['side'].upper()} {t['symbol']}: closing at market ({why})")


def manage(state, frames):
    """Update, close and finish the open trades."""
    for t in list(state["trades"].values()):
        if t["status"] != "open":
            continue
        sym, sign = t["symbol"], 1 if t["side"] == "long" else -1
        age_bars = (time.time() - t["opened"]) / 900
        pos = position_size(t["pid"])
        if pos == 0 and time.time() - t["opened"] > 90:
            fills = fills_since(t["pid"], t["opened"])
            entry = avg_price(fills, "buy" if sign > 0 else "sell")
            exit_ = avg_price(fills, "sell" if sign > 0 else "buy")
            if entry is None or exit_ is None:
                t["status"] = "no_fill"
                note(f"⚠️ {t['kind']} {t['side'].upper()} {sym}: no fills found, marked closed (check Delta)")
                continue
            t["entry"], t["exit"], t["status"] = entry, exit_, "closed"
            risk = abs(entry - t["stop"]) or 1e-9
            t["pnl_R"] = sign * (exit_ - entry) / risk
            res = "target" if t["pnl_R"] > 0 else "stop"
            note(f"{'🎯' if res == 'target' else '❌'} DELTA {t['kind']} {t['side'].upper()} {sym} closed at {fmt_price(sym, exit_)} "
                 f"(entry {fmt_price(sym, entry)}): {t['pnl_R']:+.2f}R")
            log_row(id=t["id"], kind=t["kind"], symbol=sym, side=t["side"], size=t["size"], entry=entry, exit=exit_,
                    stop=t["stop"], target=t["tp"], result=res, pnl_R=f"{t['pnl_R']:.2f}")
            if t["kind"] == "main" and res == "stop" and CONTRA_ON:
                side2 = "short" if t["side"] == "long" else "long"
                ext = entry                                   # contra stop = the extreme the failed trade reached since the E bar
                df = frames.get(sym)
                if df is not None:
                    since = df[df["start"] >= pd.Timestamp(t["bar"])]
                    if len(since):
                        ext = float(since["High"].max()) if sign > 0 else float(since["Low"].min())
                s2 = max(ext, entry) if sign > 0 else min(ext, entry)
                r2 = abs(s2 - exit_)
                tp2 = exit_ - sign * CONTRA_R * r2
                open_trade(state, sym, side2, t["bar"], s2, tp2, exit_, r2 / exit_, "contra", parent=t["id"])
            continue
        if pos == 0:
            continue                                         # order placed a moment ago, not filled yet
        limit = MAIN_MAX_BARS if t["kind"] == "main" else CONTRA_MAX_BARS
        if age_bars >= limit:
            close_market(t, f"time limit {limit * 15 // 60} h")
            continue
        if t["kind"] == "main" and sym in frames:
            vp = venue_price(sym)
            new_tp = vp["last"] * (1 + sign * target_pct_now(frames[sym], t["side"]))
            if sign * (new_tp - vp["last"]) <= vp["last"] * 0.0003:
                close_market(t, "the target (EMA200 area) has been reached")
            elif abs(new_tp - t["tp"]) / t["tp"] > 0.0005:
                try:
                    edit_target(sym, t["pid"], new_tp)
                    t["tp"] = float(fmt_price(sym, new_tp))
                except Exception as e:
                    print("target update failed:", e)


def realized_today(state):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pnl = 0.0
    for t in state["trades"].values():
        if t["status"] == "closed" and datetime.fromtimestamp(t["opened"], timezone.utc).strftime("%Y-%m-%d") == day:
            pnl += t.get("pnl_R", 0.0) * RISK_USD
    return pnl


def scan(state, frames):
    lines, found = [], 0
    for sym in SYMBOLS:
        df = frames.get(sym)
        if df is None or len(df) < 260:
            print(f"{sym}: not enough candles")
            continue
        lines.append(f"{sym} bar {df['start'].iloc[-1]:%H:%M}")
        for sig in find_signals(sym, df):
            found += 1
            key = f"{sym}|{sig['side']}|{sig['bar']}"
            if key in state["seen"]:
                continue
            state["seen"][key] = time.time()
            if sig["gap_pct"] < MIN_GAP_PCT:
                note(f"⏸ {sym} {sig['side'].upper()} E ignored: EMA20 is only {sig['gap_pct']:.2f}% from EMA200 (minimum {MIN_GAP_PCT:g}%)")
                continue
            if REQUIRE_COLOR and not sig["color_ok"]:
                note(f"⏸ {sym} {sig['side'].upper()} E ignored: the E candle closed against the trade direction (colour rule)")
                continue
            if os.path.exists(KILL_FILE):
                note(f"⏸ {sym} {sig['side'].upper()} E ignored: kill file STOP exists")
                continue
            if sum(1 for t in state["trades"].values() if t["status"] == "open") >= MAX_OPEN:
                note(f"⏸ {sym} {sig['side'].upper()} E ignored: {MAX_OPEN} trades already open")
                continue
            if realized_today(state) <= -DAILY_LOSS_USD:
                note(f"⏸ {sym} {sig['side'].upper()} E ignored: daily loss limit reached")
                continue
            if sig["late"]:
                note(f"⏸ {sym} {sig['side'].upper()} E ignored: the signal bar is more than one bar old")
                continue
            vp = venue_price(sym)
            long_ = sig["side"] == "long"
            price = vp["ask"] if long_ else vp["bid"]
            sign = 1 if long_ else -1
            open_trade(state, sym, sig["side"], sig["bar"], price * (1 - sign * sig["stop_pct"]),
                       price * (1 + sign * sig["tp_pct"]), price, sig["stop_pct"], "main")
    return lines, found


def status_line(state, lines, found):
    mode = "DRY RUN" if DRY_RUN else "ORDERS ON"
    txt = (f"{ENV} {mode} | last closed 15M bar: " + ", ".join(lines)
           + (f" | {found} E signal(s) on the last 2 bars" if found else " | no new E signal"))
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | {txt}", flush=True)
    if state.get("last_status") != ", ".join(lines):        # the log gets one line per new bar, not one per minute
        state["last_status"] = ", ".join(lines)
        log_text(txt)


def once():
    state = load_state()
    frames = {}
    for sym in SYMBOLS:
        try:
            frames[sym] = candles(sym)
        except Exception as e:
            print(f"{sym}: candles failed: {e}")
    if not DRY_RUN:
        manage(state, frames)
    lines, found = scan(state, frames)
    status_line(state, lines, found)
    save_state(state)


# ---------------------------------------------------------------- tools
def check():
    print(f"env={ENV} base={BASE} dry_run={DRY_RUN} live_confirmed={LIVE_OK}")
    print(f"symbols={SYMBOLS} risk=${RISK_USD:g}/trade max_leverage={MAX_LEVERAGE} max_open={MAX_OPEN} daily_loss=${DAILY_LOSS_USD:g} "
          f"target={TARGET_ATR} ATR before EMA200 contra={CONTRA_ON} ({CONTRA_R}R) min_rr={MIN_RR} trigger={TRIGGER}")
    for sym in SYMBOLS:
        p = product(sym)
        try:
            v = venue_price(sym)
            print(f"{sym}: id {p['id']} tick {p['tick_size']} contract_value {p['contract_value']} default_leverage {p.get('default_leverage')} "
                  f"| {ENV} price {v['last']} (bid {v['bid']} ask {v['ask']}, spread {(v['ask'] - v['bid']) / v['last'] * 100:.3f}%)")
        except Exception as e:
            print(f"{sym}: id {p['id']} | no price on {ENV}: {e}")
    try:
        res = private("GET", "/v2/wallet/balances")
        for w in res.get("result", []):
            if float(w.get("balance") or 0) or float(w.get("available_balance") or 0):
                print("wallet", w.get("asset_symbol"), "balance", w.get("balance"), "available", w.get("available_balance"))
    except Exception as e:
        print("wallet check:", e)


def status():
    st = load_state()
    for t in st["trades"].values():
        print(t["id"], t["status"], t.get("pnl_R"))
    print(len(st["trades"]), "trades;", "realized today $%.2f" % realized_today(st))


def test_order():
    if ENV != "testnet":
        sys.exit("--test-order works on testnet only")
    if DRY_RUN:
        sys.exit("set DELTA_DRY_RUN=0 to send the test order (testnet money)")
    sym = "BTCUSD"
    vp = venue_price(sym)
    pid = product(sym)["id"]
    private("POST", f"/v2/products/{pid}/orders/leverage", body=dict(leverage="5"))
    body = dict(product_id=pid, size=1, side="buy", order_type="market_order",
                bracket_stop_loss_price=fmt_price(sym, vp["last"] * 0.98), bracket_take_profit_price=fmt_price(sym, vp["last"] * 1.02),
                bracket_stop_trigger_method=TRIGGER)
    res = private("POST", "/v2/orders", body=body)["result"]
    print("order placed:", json.dumps(res)[:400])
    time.sleep(3)
    print("position size:", position_size(pid))
    try:
        o = edit_target(sym, pid, vp["last"] * 1.03)
        print("target edit: ok (was", o.get("stop_price"), ")")
    except Exception as e:
        print("target edit failed:", e)
    private("POST", "/v2/orders", body=dict(product_id=pid, size=1, side="sell", order_type="market_order", reduce_only="true"))
    time.sleep(3)
    print("position size after close:", position_size(pid))


def live_test():
    """One real 1-contract BTCUSD round trip: proves the signature, the bracket order, the bracket edit, the close and the
    clean-up on the real account for a few cents of cost. Always tries to leave the account flat."""
    if ENV != "live" or not LIVE_OK:
        sys.exit("--live-test needs DELTA_ENV=live and DELTA_LIVE_CONFIRM=YES-I-AM-TRADING-REAL-MONEY")
    if DRY_RUN:
        sys.exit("set DELTA_DRY_RUN=0 to send the live test order")
    sym, lev = "BTCUSD", 10
    pid = product(sym)["id"]
    if position_size(pid) != 0:
        sys.exit("you already have a BTCUSD position: close it first, the test only runs on a flat product")
    cv = float(product(sym)["contract_value"])
    vp = venue_price(sym)
    print(f"1 contract = {cv} BTC = about {vp['ask'] * cv:.2f} USD; margin at {lev}x about {vp['ask'] * cv / lev:.2f} USD; "
          f"bracket stop/target 2% away")
    prev = None
    try:
        prev = (private("GET", f"/v2/products/{pid}/orders/leverage").get("result") or {}).get("leverage")
    except Exception as e:
        print("could not read the current leverage:", e)
    t0 = time.time()
    private("POST", f"/v2/products/{pid}/orders/leverage", body=dict(leverage=str(lev)))
    body = dict(product_id=pid, size=1, side="buy", order_type="market_order",
                bracket_stop_loss_price=fmt_price(sym, vp["ask"] * 0.98), bracket_take_profit_price=fmt_price(sym, vp["ask"] * 1.02),
                bracket_stop_trigger_method=TRIGGER)
    res = private("POST", "/v2/orders", body=body)["result"]
    print("order placed: id", res.get("id"), "state", res.get("state"), "avg fill", res.get("average_fill_price"), "commission", res.get("paid_commission"))
    try:
        time.sleep(3)
        print("position size:", position_size(pid))
        for o in (private("GET", "/v2/orders", dict(product_ids=str(pid), states="open,pending")).get("result") or []):
            print("open order:", o.get("id"), o.get("stop_order_type"), o.get("order_type"), "stop_price", o.get("stop_price"),
                  "limit_price", o.get("limit_price"), "size", o.get("size"), "reduce_only", o.get("reduce_only"))
        try:
            o = edit_target(sym, pid, vp["ask"] * 1.03)
            print("target edit: ok (was", o.get("stop_price"), ")")
        except Exception as e:
            print("target edit FAILED:", e)
    finally:
        if position_size(pid) != 0:
            private("POST", "/v2/orders", body=dict(product_id=pid, size=1, side="sell", order_type="market_order", reduce_only="true"))
        time.sleep(3)
        print("position size after close:", position_size(pid), "(must be 0)")
        for o in (private("GET", "/v2/orders", dict(product_ids=str(pid), states="open,pending")).get("result") or []):
            try:
                private("DELETE", "/v2/orders", body=dict(id=o["id"], product_id=pid))
                print("cancelled leftover order", o["id"], o.get("stop_order_type") or o.get("order_type"))
            except Exception as e:
                print("could not cancel leftover order", o.get("id"), e)
        if prev:
            try:
                private("POST", f"/v2/products/{pid}/orders/leverage", body=dict(leverage=str(prev)))
                print("leverage restored to", prev)
            except Exception as e:
                print("could not restore leverage:", e)
        for f in fills_since(pid, t0):
            print("fill:", f.get("side"), "size", f.get("size"), "price", f.get("price"), "commission", f.get("commission"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test-order", action="store_true")
    ap.add_argument("--live-test", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
    elif a.once:
        once()
    elif a.status:
        status()
    elif a.test_order:
        test_order()
    elif a.live_test:
        live_test()
    else:
        sys.exit("use --check, --once, --status, --test-order or --live-test")
