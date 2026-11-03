"""Read-only eToro check: finds the five scanner markets in eToro's instrument list.
Uses only the Market Data read permission. Places no orders. Prints instrument info only (never the keys)."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "https://public-api.etoro.com"
WANT = {
    "NAS100": ["Nasdaq 100", "NSDQ100", "NAS100", "US100"],
    "XAUUSD": ["Gold", "XAUUSD"],
    "SILVER": ["Silver", "XAGUSD"],
    "BTCUSD": ["Bitcoin", "BTC"],
    "ETHUSD": ["Ethereum", "ETH"],
}


def get(path, params):
    key, user = os.environ.get("ETORO_PUBLIC_KEY", ""), os.environ.get("ETORO_PRIVATE_KEY", "")
    if not key or not user:
        raise SystemExit("ETORO_PUBLIC_KEY / ETORO_PRIVATE_KEY secrets are not set")
    req = urllib.request.Request(
        f"{BASE}{path}?{urllib.parse.urlencode(params)}",
        headers={"x-api-key": key, "x-user-key": user, "x-request-id": str(uuid.uuid4()), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "replace")[:300]}


def main():
    for market, terms in WANT.items():
        print(f"\n== {market}")
        for term in terms:
            res = get("/api/v2/market-data/instruments/search", {"query": term, "limit": 5})
            if "error" in res:
                print(f"  '{term}': {res['error']} {res.get('body', '')}")
                continue
            items = res.get("results") or []
            print(f"  '{term}': {len(items)} result(s)")
            for it in items:
                print(f"     id={it.get('instrumentId')}  symbol={it.get('symbol')}  name={it.get('displayName')}  type={it.get('type')}")


if __name__ == "__main__":
    main()
