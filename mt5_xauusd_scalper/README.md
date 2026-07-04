# MT5 XAUUSD Scalper MVP

A technical-analysis-only scalping bot for **Gold (XAUUSD)** on MetaTrader 5, built
for a 25,000 USD account with strict daily risk governance.

> **This software is for technical automation and testing.**
> **Trading carries financial risk.**
> **Use demo mode first.**
> **Past performance does not guarantee future results.**
> **Live auto trading is disabled by default.**

---

## 1. What the bot does

- Trades **only XAUUSD** (aliases `XAUUSD`, `GOLD`, `XAUUSDm` supported) through the
  official MetaTrader5 Python package.
- Uses pure technical analysis: EMA, RSI, ATR, VWAP, market structure, candle anatomy.
- Classifies the market regime (TREND_UP, TREND_DOWN, RANGE, CHOPPY, SPIKE, DEAD_MARKET)
  and refuses to trade in CHOPPY, SPIKE and DEAD_MARKET conditions.
- Runs two strategies and picks the better signal per candle:
  - **High Precision Scalp** — pullback + rejection candle in a clean trend, RR 1.0.
  - **Momentum Scalp** — M5 structure break + M1 retest continuation, RR 1.6.
- Enforces a **Daily Risk Governor**: profit target lock, profit protection zone,
  max daily loss, max trades/day, consecutive-loss lock, open-loss block.
- Manages open positions: breakeven at 0.7R, 50% partial close at 1.0R, time exits.
- Sends Telegram signals/notifications and supports approve/reject buttons.
- Journals every signal, trade, daily stat and event to SQLite (`data/trading_bot.db`).
- Ships with a backtester and a parameter optimizer.

## 2. What the bot does NOT do

- No fundamental analysis, no news sentiment, no AI/ML prediction.
- No browser automation, no screen clicking, no mouse/keyboard control.
- No martingale, no grid, no averaging down, no trading without a stop loss.
- No forcing trades to reach the daily target.
- It does not promise profit. Nothing here is financial advice.

## 3. Safety rules (hard-coded)

- Every order carries SL and TP. Config refuses `require_stop_loss: false`.
- Max **1** open XAUUSD position, max **4** trades/day, stop after **2** consecutive losses.
- Daily loss limit **$250** (realized or equity drawdown) locks the day.
- Open loss above **$150** blocks new trades.
- Risk is **never increased after a loss** ($50 after a loss, $75 default,
  $100 after a win only for setups scoring ≥ 8).
- Auto trading on a **live** account is blocked unless `LIVE_AUTO` mode *and*
  `trading.allow_live_auto: true` are both explicitly set.

## 4. Why $200/day is a cap, not a forced target

The daily target is a **stop condition, not a goal-seeking mechanism**. When realized
profit reaches $200 the bot locks trading for the rest of the day — it never sizes up,
never revenge-trades and never "chases the remainder". From $150 (the profit-lock
zone), only one more trade is allowed, at **half risk** and only for a setup scoring
≥ 9/11. Protecting capital and banked profit always outranks reaching the target.
On many days the bot will simply not trade at all; that is correct behavior.

## Analytics dashboard

A local web dashboard for visual analysis of the bot's work — no new
dependencies (stdlib HTTP server; charts via Chart.js CDN in the browser):

```bat
run_dashboard.bat
```

or `python src/dashboard.py`, then open **http://127.0.0.1:8765**. It reads the
live journal (`data/trading_bot.db`) read-only, so it can run alongside the bot,
and auto-refreshes every 10 seconds. To inspect a backtest instead:

```bat
python src\backtest.py --days 90 --export-trades data\trades.csv
python src\dashboard.py --csv data\trades.csv
```

What it shows: KPI tiles (net P/L, today's P/L, win rate, profit factor,
expectancy, day-lock badge), equity curve, daily P/L bars, wins-vs-losses,
net P/L by hour of day (feed `sessions.allowed_hours` from this), by strategy,
by regime, by signal score, trade-1-vs-trade-2 comparison, the signals funnel
(sent → approved → executed / rejected / expired), plus recent-trades and
bot-events tables. Light and dark theme follow your OS setting.

## Trade geometry & cost efficiency (why the defaults look like they do)

At realistic XAUUSD costs (~30 points round trip: spread + slippage + commission),
tight M1-scalp targets are structurally unprofitable — a 20-point TP pays 30 points
of cost on every "winner". The defaults therefore enforce cost-efficient geometry:

- **M5 stop geometry** (`strategy.sl_timeframe: M5`): entries still time on M1,
  but SL/TP are sized from M5 ATR and M5 swings, so fixed costs drop to ~15-20%
  of the target instead of exceeding it.
- **Sniper targets at 1.5R** (`tp_r: 1.5`, bounds 1.2-1.8): break-even win rate
  ~48-55% after costs, versus mathematically impossible at the old 0.5R/M1 combo.
- **Cost gate** (`trading.max_cost_to_tp_pct: 25`): any trade whose estimated
  round-trip cost exceeds 25% of its TP distance is skipped and journaled —
  structurally losing trades never execute, regardless of signal quality.
- **No winner-scratching**: breakeven at 0.55R and partial at 1.0R (was 0.25R /
  0.35R) so normal gold noise doesn't convert would-be winners into scratches;
  time exits stretched to 15/25 minutes to fit the M5 pace.
- **Data-driven hours** (`sessions.allowed_hours`): after a real backtest, copy
  the historically positive hours from the `pnl_by_hour` breakdown into this list
  and the bot trades only those hours.
- **Sniper geometry optimizer**: `python src/optimizer.py --days 30 --sniper-grid`
  grids over tp_r / SL bounds / breakeven / partial / time exits / cost gate —
  the levers that control net expectancy. Walk-forward validation automatically
  tunes this same grid when sniper mode is enabled.

## Validation & honest profitability assessment

A backtest that shows profit means nothing until it survives realistic costs and
out-of-sample testing. This project ships the machinery to find the truth:

**Realistic costs (backtest.py + cost_model.py).** Every simulated fill pays a
variable spread, per-side commission, adverse slippage, and occasional requotes;
bars that span both stop and target resolve stop-first by default. The report
now includes `total_commission`, `cost_drag_pct`, and per-trade Sharpe/Sortino.
Backtest position management runs through the **same** `decide_actions()` the live
bot uses, so the two cannot silently diverge.

```bat
python src/backtest.py --days 90 --export-trades data\trades.csv
```

**Validation suite (validation.py).**

```bat
python src/validation.py --days 180 --all
```

- **Walk-forward** — optimizes on each train block, scores the *next* unseen
  block, and stitches the test blocks into one out-of-sample equity curve.
  In-sample profit is not evidence; only the OOS number counts.
- **Monte Carlo** — bootstrap-resamples the trade sequence to give confidence
  intervals for net PnL and drawdown, the probability of a losing run, and the
  probability of breaching a daily-loss-sized drawdown. Exposes results that
  only looked good because winners happened to cluster.
- **Spread sensitivity** — re-runs across a range of spreads and reports the
  break-even spread. Critical for gold, where spread alone can kill the edge.

**Go/No-Go gate (go_no_go.py).**

```bat
python src/go_no_go.py --days 180
```

Applies strict, non-negotiable criteria (≥100 out-of-sample trades, OOS profit
factor ≥ 1.2 after costs, Monte-Carlo probability of loss ≤ 30%, drawdown-breach
probability ≤ 20%, break-even spread ≥ 22 points). A single failure is a
**NO-GO** — and that is the system working: it means the data does not show a
durable, cost-adjusted edge, so you should not risk money. The command exits
non-zero on NO-GO so it can gate automation.

> No technical-analysis strategy guarantees profit. These tools give the strategy
> its best honest shot and tell you the truth — they do not manufacture an edge.

## SNIPER_MODE (enabled by default)

Sniper mode caps the day at **2 trades** while keeping only one position open at a
time — and it never forces the second trade. Setups are re-scored on a **13-point
scale**: the 11-point base setup score plus a 2-point bonus when a liquidity sweep
of the prior range (max depth `max_sweep_atr` × ATR, within `sweep_lookback_candles`
candles) was reclaimed.

| Rule | Value |
|---|---|
| Signal threshold | score ≥ 10/13 (below 11 = information only, never traded) |
| First trade executes at | score ≥ 11/13, risk $100 |
| Second trade after a **win** | score ≥ 11/13, risk $100, target/lock/session/regime/spread re-checked |
| Second trade after a **loss** | **blocked by default** (day locks). With `allow_second_trade_after_loss: true`: score ≥ 12/13 and risk $50 |
| Daily locks | +$200 target, −$150 daily loss, 2 trades taken, open loss > $150 |
| Green-day protection | once realized ≥ $100, the next trade's risk never exceeds banked profit |
| Trade management | TP 0.5R (0.35–0.6 bounds), breakeven at 0.25R, 50% partial at 0.35R, time exit 5 min, hard exit 8 min, SL 0.7–1.8 ATR |

Every Telegram signal shows `Trade number today: 1/2` (or `2/2`), and `/status`
shows trades today as `X/2`, the first trade's result, whether a second trade is
allowed and why not if blocked.

The backtest reports first-vs-second trade win rate, profit factor, expectancy,
days with 0/1/2 trades and how often the second trade improved or reduced the
daily result. To decide whether the second trade earns its place at all:

```bat
python src/optimizer.py --days 10 --compare-trade-counts
```

This compares `max_trades_per_day` 1 vs 2 and **rejects the 2-trade setting** if
the second trade has a lower profit factor than the first, inflates drawdown,
causes more max-loss days, or drags expectancy down.

Set `sniper_mode.enabled: false` in `config.yaml` to fall back to the standard
`daily_goals` profile (4 trades/day, 2-loss lock).

## 5. Setup

### Requirements

- Windows (the MetaTrader5 Python package is Windows-only)
- Python **3.11**
- MetaTrader 5 terminal with an account (start with **demo**)

### Steps

1. **Install Python 3.11** from python.org (check "Add to PATH").
2. **Create the virtual environment** and install dependencies:

   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Install MT5**: download MetaTrader 5 from your broker, install and start it.
4. **Login to MT5** with your (demo) account inside the terminal.
5. **Enable algo trading** in the terminal: Tools → Options → Expert Advisors →
   "Allow algorithmic trading" (the button "Algo Trading" must be green).
6. **Configure `.env`**:

   ```bat
   copy .env.example .env
   ```

   Fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (create a bot via @BotFather,
   get your chat id via @userinfobot) and optionally `MT5_LOGIN` / `MT5_PASSWORD` /
   `MT5_SERVER` / `MT5_PATH` (leave empty to use the already-logged-in terminal).
7. **Configure `config.yaml`** — the defaults match the 25k account plan.
   Adjust `allowed_symbol_aliases` if your broker names gold differently.

### Run (SIGNAL_ONLY — default)

```bat
python src/main.py
```

or double-click `run.bat`. The bot scans and sends Telegram signals; it never trades.

### Switch to SEMI_AUTO

Edit `config.yaml`:

```yaml
mode: SEMI_AUTO
```

Restart the bot. Signals now arrive in Telegram with **Approve** / **Reject** buttons.

## 6. How Telegram approval works (SEMI_AUTO)

1. A signal message arrives with Approve/Reject buttons.
2. Press **Approve** within **60 seconds** — otherwise the signal expires.
3. Before executing, the bot re-checks: spread, price drift from the signal entry
   (max 20 points), risk limits, and that no position opened meanwhile.
   If anything changed, the trade is cancelled and journaled.
4. **Reject** cancels the signal immediately.

### Telegram commands

`/status` `/pause` `/resume` `/kill` `/mode` `/summary` `/help`

`/kill` pauses the bot; it closes the open position only if
`trading.allow_kill_close_position: true`.

## 7. How to run the backtest

```bat
python src/backtest.py --days 30
```

Pulls 30 days of M1 from the connected MT5 terminal (M5/M15 are resampled from M1),
replays the exact live strategy + governor stack, and prints trades, win rate,
profit factor, expectancy, drawdown, per-session/hour/strategy/regime breakdowns,
days hitting +$200, days hitting max loss, and more.

Without MT5 you can feed a CSV (`time,open,high,low,close,tick_volume`):

```bat
python src/backtest.py --csv data\m1.csv
```

## 8. How to run the optimizer

```bat
python src/optimizer.py --days 10 --sample 40
```

Tests parameter combinations (RR, score minimums, spread caps, pullback tolerances,
breakeven/partial/time-exit settings). The full grid is 59,049 combos — `--sample`
draws a seeded random subset. Results go to `data/optimizer_results.csv`.
Combinations failing the safety filters (profit factor < 1.2, < 100 trades,
\> 4 consecutive losses, frequent max-loss days, loss/win imbalance) are rejected
outright — the optimizer never rewards win rate alone.

## 9. How to read the logs

- Console shows INFO-level activity.
- `logs/bot.log` holds full DEBUG history (rotated at 10 MB, kept 14 days):
  connection events, regime per candle, every signal/rejection with its reason,
  every risk block, order_check/order_send results, position updates, daily locks.
- `data/trading_bot.db` (SQLite) holds `signals`, `trades`, `daily_stats`,
  `bot_events` — open it with any SQLite browser.

## 10. How to stop the bot

- Press **Ctrl+C** in the console (clean shutdown: Telegram and MT5 are closed), or
- send `/kill` in Telegram (emergency pause), or
- close the terminal window. Open positions keep their SL/TP on the broker side.

## 11. Why LIVE_AUTO is disabled by default

Unattended live execution is the highest-risk mode: slippage, spread widening,
requotes, connection drops and configuration mistakes cost real money immediately.
The bot therefore requires **two deliberate steps** — `mode: LIVE_AUTO` *and*
`trading.allow_live_auto: true` — and refuses to start otherwise. Validate the
strategy in SIGNAL_ONLY, then SEMI_AUTO, then DEMO_AUTO for weeks before even
considering it. Capital protection beats convenience.

## Tests

```bat
python -m pytest tests -q
```

138 tests cover indicators, VWAP (incl. daily/timezone reset), regime detection,
both strategies, the selector, risk manager, lot sizing, the daily risk governor
(including the scenario behaviors from the spec), position management, SNIPER_MODE,
the realistic cost model, the shared decision core, the validation tooling
(Monte Carlo, spread sensitivity), the Go/No-Go gate, and a full end-to-end
integration suite that drives the order lifecycle through a fake MT5 terminal.

## Project structure

```
mt5_xauusd_scalper/
  README.md  requirements.txt  .env.example  config.yaml  run.bat
  src/
    main.py              # entry point + scan loop
    config.py            # pydantic config validation (.env + config.yaml)
    mt5_connector.py     # MetaTrader5 wrapper (init/login/symbol/data/orders)
    market_data.py       # candle fetching, new-candle detection
    indicators.py        # EMA/RSI/ATR/VWAP/swings/structure/candle filters
    regime_detector.py   # TREND_UP/DOWN, RANGE, CHOPPY, SPIKE, DEAD_MARKET
    strategy_high_precision.py / strategy_momentum.py / strategy_selector.py
    risk_manager.py      # lot sizing + hard per-trade checks
    daily_risk_governor.py
    execution.py         # order_check/order_send, retcodes, pre-trade rechecks
    position_manager.py  # breakeven / partial close / time exits
    telegram_bot.py      # notifications, approve/reject, commands
    journal.py           # SQLite persistence
    backtest.py  optimizer.py  models.py  utils.py
  tests/                 # 78 pytest tests
  data/                  # SQLite DB + optimizer output (created at runtime)
  logs/                  # bot.log (created at runtime)
```
