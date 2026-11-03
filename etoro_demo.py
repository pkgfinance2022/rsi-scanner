"""eToro DEMO ordering for the 5-minute E paper trades.

DEMO ONLY: every URL below contains /demo/. There is no real-account code path in this file.
Needs the repository secrets ETORO_PUBLIC_KEY and ETORO_PRIVATE_KEY (Trading-Demo write + Market Data read).

Usage:
    python etoro_demo.py --check                 print eligibility, leverage, minimum size and live spread for the 5 markets
    python etoro_demo.py --test-order            place ONE tiny demo order (NAS100, long) to verify the order call
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "https://public-api.etoro.com"
IDS = {"NAS100": 28, "XAUUSD": 18, "SILVER": 19, "BTCUSD": 100000, "ETHUSD": 100001}
NAME = {28: "NSDQ100", 18: "GOLD", 19: "SILVER", 100000: "BTC", 100001: "ETH"}
DEMO_RISK_USD = float(os.environ.get("ETORO_DEMO_RISK", "5"))        # money at risk per trade (virtual)
DEMO_MAX_AMOUNT = float(os.environ.get("ETORO_DEMO_MAX_AMOUNT", "100"))  # max margin per trade, mimics a small account


def _call(method, path, params=None, body=None, request_id=None):
    key, user = os.environ.get("ETORO_PUBLIC_KEY", ""), os.environ.get("ETORO_PRIVATE_KEY", "")
    if not key or not user:
        raise RuntimeError("ETORO_PUBLIC_KEY / ETORO_PRIVATE_KEY are not set")
    if "/demo/" not in path and not path.startswith("/api/v2/market-data/"):
        raise RuntimeError(f"refusing non-demo path: {path}")      # safety: never touch the real account
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": key, "x-user-key": user, "x-request-id": request_id or str(uuid.uuid4()),
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "rsi-scanner/1.0 (personal paper-trading tool)"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def eligibility(iid):
    res = _call("POST", "/api/v2/trading/info/demo/eligibility", body={"instrumentIds": [iid], "currency": "USD"})
    items = res.get("eligibilities") or []
    return items[0] if items else None


def rates(iid):
    res = _call("GET", "/api/v2/market-data/rates", params={"instrumentIds": str(iid)})
    items = res.get("results") or []
    return items[0] if items else None


def lookup(order_id):
    return _call("GET", "/api/v2/trading/info/demo/orders:lookup", params={"orderId": order_id})


def _cfd_config(elig, direction):
    for cfg in elig.get("leverageConfigs") or []:
        if str(cfg.get("settlementType", "")).lower() == "cfd" and str(cfg.get("direction", "")).lower() == direction:
            return cfg
    return None


def _round(x):
    return round(x, 2) if x >= 100 else round(x, 5)


def place(name, side, stop_pct, target_pct, trade_id, risk_usd=None):
    """Open a demo CFD with stop and target. stop_pct / target_pct are price distances as fractions (0.01 = 1%).
    Returns a dict that is stored in the paper ledger (and never raises)."""
    risk_usd = risk_usd or DEMO_RISK_USD
    out = dict(status="error", error=None)
    try:
        iid = IDS[name]
        elig = eligibility(iid)
        if not elig or not elig.get("allowOpenPosition"):
            out["error"] = "eToro does not allow opening this instrument right now"
            return out
        direction = "long" if side == "long" else "short"
        cfg = _cfd_config(elig, direction)
        if not cfg:
            out["error"] = f"no CFD {direction} option on eToro for {name} (short may be unavailable for your account)"
            return out
        rate = rates(iid)
        if not rate or rate.get("ask") is None or rate.get("bid") is None:
            out["error"] = "no live price from eToro (market closed?)"
            return out
        ref = float(rate["ask"] if side == "long" else rate["bid"])
        notional = risk_usd / stop_pct
        levs = sorted(int(v) for v in cfg.get("leverageValues") or [1])
        lev = next((v for v in levs if notional / v <= DEMO_MAX_AMOUNT), levs[-1])   # lowest leverage that fits the margin cap
        amount = min(notional / lev, DEMO_MAX_AMOUNT)
        if amount < float(cfg.get("minPositionAmount") or 0):
            out["error"] = f"position ${amount:.0f} is below eToro's minimum ${cfg.get('minPositionAmount')}: skipped"
            return out
        sign = 1 if side == "long" else -1
        sl, tp = _round(ref * (1 - sign * stop_pct)), _round(ref * (1 + sign * target_pct))
        body = dict(action="open", transaction="buy" if side == "long" else "sellShort", instrumentId=iid,
                    settlementType="cfd", orderType="mkt", leverage=lev, amount=round(amount, 2), orderCurrency="usd",
                    stopLossRate=sl, takeProfitRate=tp, stopLossType="fixed")
        rid = str(uuid.uuid5(uuid.NAMESPACE_URL, "rsi-scanner-demo|" + trade_id))     # same trade -> same id -> no duplicates
        res = _call("POST", "/api/v2/trading/execution/demo/orders", body=body, request_id=rid)
        out.update(orderId=res.get("orderId"), amount=round(amount, 2), leverage=lev, exposure=round(amount * lev, 2),
                   ref=ref, stop=sl, target=tp)
        for _ in range(6):
            time.sleep(2)
            info = lookup(res["orderId"])
            st = info.get("status") or {}
            out["status"] = st.get("name", "unknown")
            if st.get("id") in (3, 5):
                pos = (info.get("positionExecutions") or [{}])[0]
                out.update(positionId=pos.get("positionId"), fill=(pos.get("openingData") or {}).get("avgPrice"))
                break
            if st.get("id") in (4, 7, 8, 10):
                out["error"] = f"{st.get('errorCode')} {st.get('errorMessage')}"
                break
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


def describe(name, side, res):
    tag = f"{'BUY' if side == 'long' else 'SELL (short)'} {NAME.get(IDS[name], name)}"
    if res.get("error") and not res.get("orderId"):
        return f"🧪 eToro DEMO order NOT placed ({tag}): {res['error']}"
    txt = (f"🧪 eToro DEMO order (virtual money): {tag} | margin ${res.get('amount')} x{res.get('leverage')} "
           f"(exposure ${res.get('exposure')}) | stop {res.get('stop')} | target {res.get('target')} | status {res.get('status')}")
    if res.get("fill"):
        txt += f" at {res['fill']}"
    if res.get("error"):
        txt += f" | {res['error']}"
    return txt


def check():
    for name, iid in IDS.items():
        print(f"\n== {name} (eToro {NAME[iid]}, id {iid})")
        try:
            el = eligibility(iid)
            print(f"  allowOpenPosition={el.get('allowOpenPosition')}  minPositionExposure={el.get('minPositionExposure')}  "
                  f"allowTrailingStopLoss={el.get('allowTrailingStopLoss')}")
            for cfg in el.get("leverageConfigs") or []:
                print(f"  {str(cfg.get('settlementType')).lower():<6} {str(cfg.get('direction')).lower():<6} leverage {cfg.get('leverageValues')}  "
                      f"minAmount {cfg.get('minPositionAmount')}  SL% {cfg.get('minStopLossPercentage')}-{cfg.get('maxStopLossPercentage')}  "
                      f"TP% {cfg.get('minTakeProfitPercentage')}-{cfg.get('maxTakeProfitPercentage')}")
            r = rates(iid)
            if r and r.get("bid") and r.get("ask"):
                mid = (r["bid"] + r["ask"]) / 2
                print(f"  bid {r['bid']}  ask {r['ask']}  spread {(r['ask'] - r['bid']) / mid * 100:.3f}% of price  ({r.get('quoteType')})")
        except Exception as e:
            print("  error:", e)


def test_order():
    res = place("NAS100", "long", 0.01, 0.01, "manual-test-" + str(uuid.uuid4()), risk_usd=1.0)
    print(json.dumps(res, indent=1))
    print(describe("NAS100", "long", res))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test-order", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
    elif a.test_order:
        test_order()
    else:
        sys.exit("use --check or --test-order")
