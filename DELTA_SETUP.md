# Delta Exchange bot: setup

`delta_bot.py` trades the 15M E signal on Delta Exchange (BTCUSD, ETHUSD, XAUTUSD by default).
It starts in **dry run on testnet**: it only prints and sends Telegram messages. Nothing reaches Delta until you switch it on, step by step.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DELTA_ENV` | `testnet` | `testnet` or `live` |
| `DELTA_DRY_RUN` | `1` | `0` = really send orders |
| `DELTA_LIVE_CONFIRM` | empty | must be exactly `YES-I-AM-TRADING-REAL-MONEY` for `live` |
| `DELTA_API_KEY` / `DELTA_API_SECRET` | none | keys for the environment above. Never put them in the repo or in chat |
| `DELTA_SYMBOLS` | `BTCUSD,ETHUSD,XAUTUSD` | allow-list |
| `DELTA_RISK_USD` | `2` | money lost if the stop is hit (about; contracts are whole numbers) |
| `DELTA_MAX_LEVERAGE` | `10` | the bot also keeps the stop inside half of the liquidation distance |
| `DELTA_MAX_OPEN` | `2` | trades open at once |
| `DELTA_DAILY_LOSS_USD` | `3 x risk` | no new entries after this loss in a UTC day |
| `DELTA_TARGET_ATR` | `0.5` | exit this many ATR before EMA200 |
| `DELTA_CONTRA` / `DELTA_CONTRA_R` | `1` / `2` | contra trade after a stop, exit at 2R |
| `DELTA_TESTNET_BASE` / `DELTA_LIVE_BASE` | India URLs | change both if your account is on Delta Global |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | none | same bot as the scanner (optional) |

Create a file named `STOP` next to the script to block all new entries (open trades are still managed).

## Steps

1. **Testnet account and key.** Create a Delta *demo* (testnet) account, then an API key there with **Trading** permission. If the key page asks for IPs, add the IP of the machine that will run the bot.
2. **Run the checks** (read-only):
   ```
   export DELTA_API_KEY=...  DELTA_API_SECRET=...
   python delta_bot.py --check
   ```
   It lists product ids, prices, spreads and your testnet balance.
3. **Test order on testnet** (opens and closes 1 BTCUSD contract with a bracket, then edits the bracket):
   ```
   DELTA_DRY_RUN=0 python delta_bot.py --test-order
   ```
4. **Dry run on live signals** (default): `python delta_bot.py --once` every minute from cron. Messages say what it *would* do.
5. **Testnet for real**: `DELTA_DRY_RUN=0` (still `testnet`). Let it run for weeks and compare `delta_trades.csv` with the paper log.
6. **Live, small** (only after 1 to 5 look right): a host with a fixed IP, a live key with that IP, `DELTA_ENV=live DELTA_DRY_RUN=0 DELTA_LIVE_CONFIRM=YES-I-AM-TRADING-REAL-MONEY`, and a small `DELTA_RISK_USD`.

## Cron (every minute)

```
* * * * * cd /home/user/rsi-scanner && /usr/bin/env $(cat .env | xargs) python3 delta_bot.py --once >> delta.log 2>&1
```

## Known limits

* Delta needs whitelisted IPs for trading keys, so this cannot run on GitHub Actions.
* The exact behaviour of editing a bracket on an open position was read from the API reference, not yet seen on a real account. `--test-order` checks it. If it fails the bot keeps the target set at entry.
* Signals come from Delta's own 15M candles, so they can differ slightly from your TradingView chart and from the Telegram scanner (Yahoo data).
* Fees and slippage are not in the backtests. Round-trip cost above about 0.1% removes the tested edge.

## Live test without a testnet account

`python delta_bot.py --live-test` places **one real 1-contract BTCUSD order** (about 65 USD of exposure, margin about 6.5 USD at 10x)
with a 2% bracket, edits the bracket, closes it, cancels any leftover orders and restores your leverage setting.
It refuses to run unless `DELTA_ENV=live`, `DELTA_DRY_RUN=0` and `DELTA_LIVE_CONFIRM=YES-I-AM-TRADING-REAL-MONEY` are all set,
and it stops if you already hold a BTCUSD position. Expected cost: fees and spread, a few cents. Worst case: the 2% stop, about 1.3 USD.
Run `python delta_bot.py --check` first (read-only, also works with DELTA_ENV=live).
