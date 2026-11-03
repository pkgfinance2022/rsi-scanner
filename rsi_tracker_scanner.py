#!/usr/bin/env python3
"""
RSI Setup Tracker scanner (4H, Daily, Weekly) -> Telegram.

Python port of the TradingView long/short trackers (B+D, S, E / S+D, R, E, plus green/red line alerts).
Data: Yahoo Finance. 4H bars are rebuilt from 1h data; Daily and Weekly use Yahoo daily/weekly bars.
Only CLOSED bars are evaluated.

Each alert carries: risk to the line, distance to EMA200 (and reward/risk), a 0-5 quality score,
and the status of the same side on the other timeframes (multi-timeframe agreement).
A daily summary of all active lines is sent once a day, and every alert is appended to alert_log.csv.

Setup (once):   py -m pip install yfinance pandas numpy
Telegram creds: environment variables TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (never hardcode them here).
Test run:       py rsi_tracker_scanner.py --dry-run
Real run:       py rsi_tracker_scanner.py          (schedule it hourly; duplicates are suppressed)
"""
import argparse
import csv
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- tickers
TICKERS_CFG = {
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SPX": "^GSPC",
    "NAS100": "^NDX",
    "DJ30": "^DJI",
    "RUSSELL2000": "^RUT",
    "GER40": "^GDAXI",
    "FRA40": "^FCHI",
    "UK100": "^FTSE",
    "JPN225": "^N225",
    "NL25": "^AEX",
    "SIGI30": "^STI",
    "IT40": "FTSEMIB.MI",

    "USDCAD": "USDCAD=X",
    "USDJPY": "USDJPY=X",
    "GBPUSD": "GBPUSD=X",
    "EURUSD": "EURUSD=X",
    "USDCHF": "USDCHF=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",

    "XAUUSD": "GC=F",
    "SILVER": "SI=F",
    "COPPER": "HG=F",
    "USOIL": "CL=F",

    "AUS200": "^AXJO",
    "CHINA50": "FXI",

    # US shares (Yahoo symbols)
    "NFLX": "NFLX", "META": "META", "DIS": "DIS", "BKNG": "BKNG", "AAPL": "AAPL",
    "GOOGL": "GOOGL", "HD": "HD", "ORCL": "ORCL", "KO": "KO", "MCO": "MCO",
    "TMUS": "TMUS", "WMT": "WMT", "AMZN": "AMZN", "LLY": "LLY", "CAT": "CAT",
    "MCD": "MCD", "ASML": "ASML", "TSM": "TSM", "SNDK": "SNDK", "MSFT": "MSFT",
    "IDXX": "IDXX", "CDNS": "CDNS", "FTNT": "FTNT", "GD": "GD", "AVGO": "AVGO",
    "NVDA": "NVDA", "PANW": "PANW", "TSLA": "TSLA", "BX": "BX", "MSTR": "MSTR",

    # Crypto (24/7)
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "SOLUSD": "SOL-USD", "XRPUSD": "XRP-USD",

    # Dollar index, yen futures, volatility indices
    "DXY": "DX-Y.NYB", "6J1!": "6J=F", "VIX": "^VIX", "INDIAVIX": "^INDIAVIX",
}

# Round-the-clock markets: 4H bars anchored to a fixed UTC hour. Everything else: anchored to each day's session open.
CONTINUOUS = {"USDCAD", "USDJPY", "GBPUSD", "EURUSD", "USDCHF", "AUDUSD", "NZDUSD",
              "XAUUSD", "SILVER", "COPPER", "USOIL", "DXY", "6J1!"}
CRYPTO = {"BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"}   # 24/7: 4H bars open 00,04,08,12,16,20 UTC; weekly bar runs Mon-Sun
FX_ANCHOR_HOUR_UTC = 1   # 4H bars open 01,05,09,13,17,21 UTC. CHECK against your TradingView chart; change if bars differ.

# ---------------------------------------------------------------- parameters (same defaults as the Pine scripts)
P = dict(
    rsi_len=28, ma_len=14, fast=20, slow=200, mid=60, atr_len=14,
    os=30.0, lvl1=45.0, lvl2=50.0, ext=20.0, ext_window=60, sustain=2,
    need_ma=True, need_ema20=True,
    watch_bars=24, fail_expiry=80, max_attempts=2, break_on_close=True,
    arm_bars=120, sup_tol_mult=0.3, sup_bull=True, keep_on_fail=True, only_bd=True,
)
SEND_EVENTS = {"B+D", "S", "E"}      # per side; add "WATCH" to also send those
LINE_ALERTS = True                   # green/red line: when it starts (B, B2), when it breaks (FAIL), when EMA200 is reached (TARGET)
if LINE_ALERTS:
    SEND_EVENTS |= {"B", "B2", "FAIL", "TARGET"}
TIMEFRAMES = ["4H", "D", "W"]
LOOKBACK = {"4H": 4, "D": 3, "W": 2, "5M": 3}   # closed bars to look back each run (dedupe prevents repeats)
FAST_TICKERS = ["BTCUSD", "ETHUSD", "XAUUSD", "SILVER"]   # --fast mode: 5-minute E alerts for these only
FAST_EVENTS = {"E"}
DEMO_EVENTS = {"B+D", "S", "E"}
DIGEST_HOUR_UTC = 6                  # daily "what's active" summary is sent by the first run at/after this UTC hour
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "scanner_state.json")
LOG_FILE = os.environ.get("ALERT_LOG", os.path.join(HERE, "alert_log.csv"))
STATUS = {0: "-", 1: "WATCH", 2: "ACTIVE", 3: "RETRY"}
DIGEST_ALIAS = {"RUSSELL2000": "RUSSELL2K"}   # keeps the daily-summary table narrow on a phone


# ---------------------------------------------------------------- indicators
def rma(s, n):
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def indicators(df):
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    src = (o + h + l + c) / 4.0                      # OHLC4, same as the Pine default
    d = src.diff()
    up = rma(d.clip(lower=0), P["rsi_len"])
    dn = rma((-d).clip(lower=0), P["rsi_len"])
    rsi = 100 - 100 / (1 + up / dn)
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return dict(
        o=o.values, h=h.values, l=l.values, c=c.values,
        rsi=rsi.values, rsima=ema(rsi, P["ma_len"]).values,
        emaf=ema(c, P["fast"]).values, emas=ema(c, P["slow"]).values,
        emam=ema(c, P["mid"]).values, atr=rma(tr, P["atr_len"]).values,
    )


def mirror(d):
    """Short side = the long logic run on mirrored prices (price -> -price, RSI -> 100-RSI)."""
    return dict(
        o=-d["o"], h=-d["l"], l=-d["h"], c=-d["c"],
        rsi=100 - d["rsi"], rsima=100 - d["rsima"],
        emaf=-d["emaf"], emas=-d["emas"], emam=-d["emam"], atr=d["atr"],
    )


# ---------------------------------------------------------------- tracker (port of the Pine state machine)
def track(d):
    """Returns (events, final). events: (bar_index, name, level, info). final: state at the last bar."""
    n = len(d["c"])
    state = 0
    attempt = 0
    cur_low = cur_rsi_low = prev_low = prev_rsi_low = np.nan
    last_os = buy_bar = break_bar = 0
    divergence = False
    armed = False
    arm_bar = 0
    arm_low = np.nan
    above200 = False
    above_bar = 0
    ext_armed = False
    ext_bar = 0
    prev_cond = False
    events = []

    def info_at(i):
        """Risk, EMA200 room and the 0-5 quality score (same checklist as the Pine line colour)."""
        c = d["c"][i]
        risk = c - cur_low
        room = d["emas"][i] - c
        k = P["sustain"]
        sustained = i >= k - 1 and bool(np.min(d["c"][i - k + 1:i + 1] - d["emaf"][i - k + 1:i + 1]) > 0)
        score = (int(divergence) + int(cur_rsi_low <= P["os"] - 5) + int(sustained)
                 + int(d["rsi"][i] >= P["lvl2"]) + int(risk > 0 and room >= 2 * risk))
        return dict(
            price=float(c), score=score, rsi_low=float(cur_rsi_low), ema200=float(d["emas"][i]),
            risk_pct=float(risk / abs(c) * 100) if risk > 0 else np.nan,
            room_pct=float(room / abs(c) * 100),
            rr=float(room / risk) if risk > 0 else np.nan,
        )

    for i in range(n):
        o, h, l, c = d["o"][i], d["h"][i], d["l"][i], d["c"][i]
        rsi, rsima = d["rsi"][i], d["rsima"][i]
        emaf, emas, emam, atr = d["emaf"][i], d["emas"][i], d["emam"][i], d["atr"][i]
        rsi_prev = d["rsi"][i - 1] if i > 0 else np.nan

        oversold = rsi <= P["os"]
        cond = (rsi > P["lvl1"]) and ((not P["need_ma"]) or rsi > rsima) and ((not P["need_ema20"]) or c > emaf)
        buy_cross = cond and not prev_cond
        prev_cond = cond

        st = state
        new_watch = new_buy = False

        if st == 0 and oversold:
            state, attempt = 1, 1
            cur_low, cur_rsi_low = l, rsi
            last_os, divergence, new_watch = i, False, True
        elif st == 1:
            cur_low = min(cur_low, l)
            cur_rsi_low = min(cur_rsi_low, rsi)
            if oversold:
                last_os = i
            if buy_cross:
                state, buy_bar, new_buy = 2, i, True
            elif i - last_os > P["watch_bars"]:
                state = 0
        elif st == 2:
            brk = (c < cur_low) if P["break_on_close"] else (l < cur_low)
            if brk:
                events.append((i, "FAIL", cur_low, None))
                state = 0 if attempt >= P["max_attempts"] else 3
                attempt += 1
                prev_low, prev_rsi_low = cur_low, cur_rsi_low
                cur_low, cur_rsi_low = l, rsi
                break_bar = i
            else:
                if h >= emas:
                    events.append((i, "TARGET", emas, None))
                    state = 0
                elif i - buy_bar > 150:
                    state = 0
        elif st == 3:
            cur_low = min(cur_low, l)
            cur_rsi_low = min(cur_rsi_low, rsi)
            if buy_cross:
                state, buy_bar, new_buy = 2, i, True
                divergence = (cur_low < prev_low) and (cur_rsi_low > prev_rsi_low)
            elif i - break_bar > P["fail_expiry"]:
                state = 0

        # Stage 2: only armed after a qualifying buy
        if new_buy and ((not P["only_bd"]) or (attempt >= 2 and divergence)):
            armed, arm_bar, arm_low, above200 = True, i, cur_low, False
        if armed and not P["keep_on_fail"] and c < arm_low:
            armed = False
        if armed and i - arm_bar > P["arm_bars"]:
            armed = False
        if armed and not above200 and c > emas and c > emam:
            above200, above_bar = True, i
        if armed and above200 and c < emas:
            above200 = False
        s2_now = (armed and above200 and i > above_bar and l <= emas + atr * P["sup_tol_mult"]
                  and c > emas and ((not P["sup_bull"]) or c > o))
        if s2_now:
            armed, above200 = False, False

        # Extreme + recovery
        if rsi <= P["ext"]:
            ext_armed, ext_bar = True, i
        ready = ext_armed and (rsi > P["lvl1"] and rsi_prev <= P["lvl1"])
        if ready or i - ext_bar > P["ext_window"]:
            ext_armed = False

        if new_watch:
            events.append((i, "WATCH", cur_low, None))
        if new_buy:
            if attempt >= 2 and divergence:
                events.append((i, "B+D", cur_low, info_at(i)))
            elif attempt >= 2:
                events.append((i, "B2", cur_low, info_at(i)))
            else:
                events.append((i, "B", cur_low, info_at(i)))
        if s2_now:
            events.append((i, "S", np.nan, None))
        if ready:
            events.append((i, "E", np.nan, None))

    final = dict(state=state, level=cur_low, attempt=attempt, divergence=divergence,
                 info=info_at(n - 1) if (state == 2 and n) else None)
    return events, final


# ---------------------------------------------------------------- data
def to_4h(df, continuous, anchor_hour):
    df = df[["Open", "High", "Low", "Close"]].dropna()
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if continuous:
        df = df.copy()
        df.index = df.index.tz_convert("UTC")
        out = df.resample("4h", offset=pd.Timedelta(hours=anchor_hour)).agg(agg).dropna()
        out["start"] = out.index
        return out
    # session markets: 4H buckets counted from each day's first bar
    day = pd.Series(df.index.date, index=df.index)
    first = pd.Series(df.index, index=df.index).groupby(day).transform("min")
    k = (df.index - first) // pd.Timedelta(hours=4)
    g = df.groupby([day.values, k.values])
    out = g.agg(agg)
    out["start"] = g.apply(lambda x: x.index.min())
    out = out.sort_values("start")
    out.index = pd.DatetimeIndex(out["start"])
    return out


def load_4h(symbol, name):
    raw = yf.Ticker(symbol).history(period="730d", interval="1h", auto_adjust=False)
    if raw is None or raw.empty:
        return None
    bars = to_4h(raw, name in CONTINUOUS or name in CRYPTO, 0 if name in CRYPTO else FX_ANCHOR_HOUR_UTC)
    now = datetime.now(timezone.utc)
    start_utc = pd.DatetimeIndex(bars["start"]).tz_convert("UTC")
    closed = start_utc + pd.Timedelta(hours=4) <= now
    return bars[closed]


def load_5m(symbol):
    raw = yf.Ticker(symbol).history(period="30d", interval="5m", auto_adjust=False)
    if raw is None or raw.empty:
        return None
    bars = raw[["Open", "High", "Low", "Close"]].dropna().copy()
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    idx = idx.tz_convert("UTC")
    bars.index = idx
    bars["start"] = idx
    return bars[idx + pd.Timedelta(minutes=5) <= pd.Timestamp.now(tz="UTC")]   # drop the bar still forming


def load_bars(symbol, name, tf):
    if tf == "4H":
        return load_4h(symbol, name)
    if tf == "5M":
        return load_5m(symbol)
    interval, period = ("1d", "10y") if tf == "D" else ("1wk", "max")
    raw = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
    if raw is None or raw.empty:
        return None
    bars = raw[["Open", "High", "Low", "Close"]].dropna().copy()
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    bars.index = idx
    bars["start"] = idx
    span = pd.Timedelta(days=1) if tf == "D" else pd.Timedelta(days=7 if name in CRYPTO else 5)   # weekly bar closed from Saturday 00:00 (crypto: next Monday)
    closed = (idx + span) <= pd.Timestamp.now(tz="UTC")
    return bars[closed]


# ---------------------------------------------------------------- telegram, state, log
def send_telegram(text, as_html=False):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables (or use --dry-run).")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    fields = {"chat_id": chat, "text": text}
    if as_html:
        fields["parse_mode"] = "HTML"
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        r.read()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)


LOG_FIELDS = ["sent_utc", "ticker", "timeframe", "side", "event", "bar_time_utc", "price", "line_level",
              "risk_pct", "ema200_room_pct", "reward_risk", "score", "same_side_status"]


def append_log(row):
    try:
        new = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"(could not write alert log: {e})")


def fmt(v):
    return f"{v:.6g}"


def ok(v):
    return v is not None and v == v      # not None and not NaN


def mtf_text(status):
    inplay = sum(1 for v in status.values() if v in ("WATCH", "ACTIVE"))
    parts = " · ".join(f"{tf} {status[tf]}" for tf in TIMEFRAMES if tf in status)
    return f"Same side: {parts} ({inplay}/{len(status)} in play)"


def message(name, tf, side, ev, level, bar_time, info=None, mtf=None):
    long_ = side == "long"
    lv = fmt(level) if level == level else ""      # level == level is False for NaN
    if long_:
        dot = {"S": "🟢", "B+D": "🟢", "B": "🟢", "B2": "🟢", "E": "🟠", "WATCH": "🟡", "FAIL": "⚠️", "TARGET": "🎯"}[ev]
        text = {
            "B+D": f"B+D: buy signal with bullish RSI divergence. Support line (protected low) at {lv}",
            "B": f"B: first buy. Support line (protected low) starts at {lv}",
            "B2": f"B2: retry buy, no divergence. Support line (protected low) at {lv}",
            "S": "S: broke above EMA200, retest held as support",
            "E": "E: RSI hit the extreme low and is back above 45, be ready",
            "WATCH": "WATCH: RSI oversold, start watching",
            "FAIL": f"Support line {lv} BROKEN (close below it): setup failed",
            "TARGET": f"Price reached EMA200 ({lv}): pull-up target hit, support line ends",
        }[ev]
    else:
        dot = {"S": "🔴", "B+D": "🔴", "B": "🔴", "B2": "🔴", "E": "🟠", "WATCH": "🟡", "FAIL": "⚠️", "TARGET": "🎯"}[ev]
        text = {
            "B+D": f"S+D: sell signal with bearish RSI divergence. Resistance line (protected high) at {lv}",
            "B": f"S: first sell. Resistance line (protected high) starts at {lv}",
            "B2": f"S2: retry sell, no divergence. Resistance line (protected high) at {lv}",
            "S": "R: broke below EMA200, retest held as resistance",
            "E": "E: RSI hit the extreme high and is back below 55, be ready",
            "WATCH": "WATCH: RSI overbought, start watching",
            "FAIL": f"Resistance line {lv} BROKEN (close above it): setup failed",
            "TARGET": f"Price reached EMA200 ({lv}): drop target hit, resistance line ends",
        }[ev]
    when = f"{bar_time:%Y-%m-%d %H:%M} UTC" if tf in ("4H", "5M") else f"{bar_time:%Y-%m-%d}"
    out = f"{dot} {name} {tf} | {text}. (bar {when})"
    extras = []
    if info and ev in ("B", "B2", "B+D"):
        bits = []
        if ok(info["risk_pct"]):
            bits.append(f"Risk to line {info['risk_pct']:.1f}%")
        if info["room_pct"] > 0:
            room = f"EMA200 {info['room_pct']:.1f}% away"
            if ok(info["rr"]):
                room += f" ({info['rr']:.1f}R)"
            bits.append(room)
        else:
            bits.append("price already beyond EMA200")
        bits.append(f"Score {info['score']}/5")
        extras.append(" | ".join(bits))
        tgt = fmt(info["ema200"] if long_ else -info["ema200"])
        what = "Support" if long_ else "Resistance"
        extras.append(f"Plan: {'buy' if long_ else 'sell'} now, trail to EMA200 ≈ {tgt}; "
                      f"setup fails if a bar closes {'below' if long_ else 'above'} the {what.lower()} line {lv}")
    if mtf and ev in ("B", "B2", "B+D", "S", "E"):
        extras.append(mtf_text(mtf))
    return out + ("\n" + "\n".join(extras) if extras else "")


def digest_table(rows):
    """Fixed-width table that fits a phone screen (about 38 characters per row)."""
    out = [f"{'Ticker':<10}{'TF':<3}{'Line':>9}{'Risk%':>6}{'EMA%':>6}{'Sc':>3}"]
    for a in rows:
        inf = a["info"]
        risk = f"{inf['risk_pct']:.1f}" if inf and ok(inf["risk_pct"]) else "-"
        room = f"{inf['room_pct']:.1f}" if inf and inf["room_pct"] > 0 else ("pass" if inf else "-")
        score = str(inf["score"]) if inf else "-"
        out.append(f"{DIGEST_ALIAS.get(a['name'], a['name']):<10}{a['tf']:<3}{fmt(a['level']):>9}{risk:>6}{room:>6}{score:>3}")
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


def build_digest(active, watching, today):
    """Returns a list of HTML messages (one per section, so none gets too long)."""
    order = {tf: k for k, tf in enumerate(TIMEFRAMES)}
    legend = "<i>Risk% = distance to the line, EMA% = distance to EMA200 (pass = already beyond), Sc = score out of 5</i>"
    msgs = [f"📋 <b>Daily summary {today:%Y-%m-%d}</b> (UTC)\n{legend}"]
    for side, title, dot in (("long", "Support lines active", "🟢"), ("short", "Resistance lines active", "🔴")):
        rows = [a for a in active if a["side"] == side]
        rows.sort(key=lambda a: (-(a["info"]["score"] if a["info"] else 0), order[a["tf"]], a["name"]))
        body = digest_table(rows) if rows else "none"
        msgs.append(f"{dot} <b>{title} ({len(rows)})</b>\n{body}")
    msgs.append(f"👀 <b>Watching</b> (RSI stretched, no signal yet): {watching['long']} oversold · {watching['short']} overbought")
    return msgs


def send_digest(msgs, dry_run):
    for m in msgs:
        print(m)
        if not dry_run:
            send_telegram(m, as_html=True)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print messages, do not send, do not save state or log")
    ap.add_argument("--only", help="comma separated names, e.g. EURUSD,SPX")
    ap.add_argument("--tf", default=",".join(TIMEFRAMES), help="timeframes to scan, e.g. 4H,D,W")
    ap.add_argument("--lookback", type=int, default=None, help="override closed bars to look back")
    ap.add_argument("--demo", action="store_true", help="send the most recent PAST signal of each kind, labelled DEMO")
    ap.add_argument("--digest", action="store_true", help="send (or with --dry-run print) the daily summary now")
    ap.add_argument("--test-telegram", action="store_true", help="send one test message and exit")
    ap.add_argument("--fast", action="store_true", help="5-minute E alerts for BTCUSD, ETHUSD, XAUUSD, SILVER only")
    args = ap.parse_args()
    if args.fast:
        args.only, args.tf = ",".join(FAST_TICKERS), "5M"

    if args.test_telegram:
        send_telegram("✅ RSI scanner (4H, D, W) connected. Test message from rsi_tracker_scanner.py")
        print("test message sent")
        return

    names = [n.strip() for n in args.only.split(",")] if args.only else list(TICKERS_CFG)
    tfs = [t.strip().upper() for t in args.tf.split(",")]
    state = load_state()
    sent = 0
    loaded = 0
    demo = []
    active = []
    watching = {"long": 0, "short": 0}
    for name in names:
        sym = TICKERS_CFG.get(name)
        if not sym:
            print(f"unknown ticker {name}")
            continue
        res = {}
        for tf in tfs:
            try:
                bars = load_bars(sym, name, tf)
                if bars is None or len(bars) < 260:
                    print(f"{name} {tf}: not enough data ({0 if bars is None else len(bars)} bars)")
                    continue
                loaded += 1
                d = indicators(bars)
                times = pd.DatetimeIndex(bars["start"]).tz_convert("UTC")
                res[tf] = dict(times=times, last=len(bars) - 1, sides={})
                for side, dd in (("long", d), ("short", mirror(d))):
                    res[tf]["sides"][side] = track(dd)
            except SystemExit:
                raise
            except Exception as e:
                print(f"{name} {tf}: error {type(e).__name__}: {e}")

        for side in ("long", "short"):
            status = {tf: STATUS[res[tf]["sides"][side][1]["state"]] for tf in res}
            for tf in res:
                final = res[tf]["sides"][side][1]
                sign = 1 if side == "long" else -1
                if final["state"] == 2:
                    active.append(dict(name=name, tf=tf, side=side, level=sign * final["level"], info=final["info"]))
                elif final["state"] == 1:
                    watching[side] += 1
                times, last = res[tf]["times"], res[tf]["last"]
                lookback = args.lookback or LOOKBACK[tf]
                for i, ev, level, info in res[tf]["sides"][side][0]:
                    if ev not in (FAST_EVENTS if args.fast else SEND_EVENTS):
                        continue
                    lvl = sign * level
                    if args.demo:
                        if ev in DEMO_EVENTS:
                            demo.append((times[i], name, tf, side, ev, lvl, info))
                        continue
                    if i < last - lookback + 1:
                        continue
                    # 4H keeps its original key format so earlier alerts are not repeated
                    key = f"{name}|{side}|{ev}" if tf == "4H" else f"{name}|{tf}|{side}|{ev}"
                    bar_iso = times[i].isoformat()
                    if state.get(key) == bar_iso:
                        continue
                    msg = message(name, tf, side, ev, lvl, times[i], info, None if args.fast else status)
                    print(msg)
                    if not args.dry_run:
                        send_telegram(msg)
                        state[key] = bar_iso
                        sent += 1
                        append_log(dict(
                            sent_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), ticker=name,
                            timeframe=tf, side=side, event=ev, bar_time_utc=times[i].isoformat(),
                            price=fmt(info["price"] * sign) if info else "", line_level=fmt(lvl) if lvl == lvl else "",
                            risk_pct=f"{info['risk_pct']:.2f}" if info and ok(info["risk_pct"]) else "",
                            ema200_room_pct=f"{info['room_pct']:.2f}" if info else "",
                            reward_risk=f"{info['rr']:.2f}" if info and ok(info["rr"]) else "",
                            score=info["score"] if info else "",
                            same_side_status=" ".join(f"{k}:{v}" for k, v in status.items())))

    if args.demo:
        # most recent past signal of each kind per timeframe and side, labelled DEMO; does not touch state or log
        latest = {}
        for t, name, tf, side, ev, lvl, info in demo:
            k = (tf, side, ev)
            if k not in latest or t > latest[k][0]:
                latest[k] = (t, name, tf, side, ev, lvl, info)
        for t, name, tf, side, ev, lvl, info in sorted(latest.values(), key=lambda x: (x[2], x[0])):
            msg = "🧪 DEMO (past signal, not live) " + message(name, tf, side, ev, lvl, t, info)
            print(msg)
            if not args.dry_run:
                send_telegram(msg)
        print(f"demo done, {len(latest)} message(s)")
        return

    # daily summary of everything currently active
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    due = (not args.only) and not args.fast and now.hour >= DIGEST_HOUR_UTC and state.get("_digest_date") != today
    if loaded and (args.digest or due):
        send_digest(build_digest(active, watching, now), args.dry_run)
        if not args.dry_run:
            state["_digest_date"] = today

    if not args.dry_run:
        save_state(state)
    total = len(names) * len(tfs)
    print(f"done, {sent} message(s) sent, {loaded}/{total} ticker-timeframes loaded" if not args.dry_run else "dry run done")
    if loaded == 0:
        raise SystemExit("No ticker data could be loaded (Yahoo may be blocking this machine).")


if __name__ == "__main__":
    main()
