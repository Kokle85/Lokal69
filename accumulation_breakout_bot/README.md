# Accumulation Breakout Bot

A standalone MetaTrader 5 Python bot that detects **accumulation zones** from
wick rejections on the **M5** timeframe and trades **confirmed breakouts** out
of those zones with a fixed **3:1 risk-to-reward** ratio.

> ⚠️ **Risk warning.** Trading leveraged CFDs can lose your entire deposit.
> Nothing here is a promise of profit. Live trading is **disabled by default**;
> validate on a demo account first, always.

## What the bot does

1. Scans the last 30 closed M5 candles for a **sideways range** whose
   boundaries were repeatedly rejected with **wicks** (default: at least 3
   upper and 3 lower wick touches within an ATR-based tolerance).
2. Rejects weak zones: too small, too large, trending windows, or windows
   where too many candle bodies close outside the range.
3. Waits for a **confirmed breakout**: an M5 candle that *closes* beyond the
   zone boundary + buffer, with a strong body and without a large opposing
   rejection wick.
4. Enters in one of two modes:
   - `DIRECT_BREAKOUT` — enter on the close of the breakout candle;
   - `BREAKOUT_RETEST` (default) — wait for price to come back to the broken
     boundary and print a rejection candle there, then enter.
5. **SL** goes beyond the opposite side of the zone (+ buffer);
   **TP = entry ± 3 × risk**. Trades with too-tight or too-wide stops are
   rejected.
6. Filters: EMA-200 trend filter, London/New York session filter (broker
   time), max spread, max 2 trades/day, 60-minute cooldown after a close, and
   a (disabled-by-default) news-block interface.
7. Position size is computed from account balance and
   `risk_per_trade_percent` using the symbol's tick value from MT5.
   **No lot → no trade. No SL/TP → no trade.**

Every signal, executed trade, and **rejection with its reason** is journaled
to `data/journals/journal_YYYYMMDD.csv`.

## Setup

### Python

```bash
cd accumulation_breakout_bot
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS (backtest only - MetaTrader5 is Windows-only):
source .venv/bin/activate
pip install -r requirements.txt
```

### MetaTrader 5 (for signal/live mode)

1. Install the MT5 terminal and log in to your broker account.
2. Enable **Tools → Options → Expert Advisors → Allow algorithmic trading**.
3. Make sure the symbol (default `XAUUSD`) is visible in Market Watch —
   brokers name gold differently (`GOLD`, `XAUUSDm`); set the exact name in
   `config/config.yaml`.
4. Leave the `mt5:` credentials empty to use the already-logged-in terminal,
   or fill in login/password/server.

## Configuration

Everything lives in `config/config.yaml`. Key parameters:

| Key | Default | Meaning |
|---|---|---|
| `entry_mode` | `BREAKOUT_RETEST` | or `DIRECT_BREAKOUT` |
| `risk_per_trade_percent` | `0.5` | % of balance risked per trade |
| `risk_reward_ratio` | `3.0` | TP distance = 3 × SL distance |
| `zone_lookback_candles` | `30` | window scanned for a zone |
| `minimum_upper/lower_wick_touches` | `3` | wick rejections required per side |
| `touch_tolerance_atr_multiplier` | `0.15` | how near a wick must come to count |
| `min/max_zone_size_atr` | `0.8 / 2.0` | zone size band in ATR |
| `min_body_inside_zone_percent` | `70` | body containment for a clean range |
| `breakout_buffer_atr_multiplier` | `0.15` | close must clear boundary + buffer |
| `min_breakout_body_atr_multiplier` | `0.4` | breakout candle body strength |
| `max_rejection_wick_percent` | `40` | opposing wick cap on the breakout candle |
| `min/max_stop_distance_atr` | `0.5 / 2.5` | allowed SL distance band |
| `use_ema_filter` / `ema_period` | `true / 200` | trade only with the EMA trend |
| `use_session_filter` / `sessions` | `true` | London 08:00–12:00, NY 13:30–17:00 (broker time) |
| `max_spread_points` | `35` | skip entries on wide spread |
| `max_trades_per_day` | `2` | daily cap |
| `cooldown_minutes` | `60` | wait after a trade closes |
| `live_trading_enabled` | **`false`** | safety gate for live mode |

## Running

### Signal mode (default-safe: no orders, ever)

```bash
python src/main.py --mode signal
```

Detects zones and breakouts, computes entry/SL/TP/lot, logs and journals
everything — and does **not** send orders.

### Backtest

```bash
# from a CSV (HistData MT format or time,open,high,low,close; M1 auto-resampled)
python src/main.py --mode backtest --csv data/historical/xau_m5.csv

# or straight from the MT5 terminal's history
python src/main.py --mode backtest --symbol XAUUSD --from 2025-01-01 --to 2026-01-01
```

The backtester runs the **comparison matrix** — `DIRECT_BREAKOUT` vs
`BREAKOUT_RETEST` × EMA filter on/off × session filter on/off (8 runs) — and
writes `data/reports/backtest_<stamp>.csv` + `.json`. Metrics per run: total
trades, wins/losses, win rate, average R, total R, profit factor, max
drawdown (R), long/short counts, best/worst trade, max consecutive
wins/losses.

### Optimize

```bash
python src/main.py --mode optimize --csv data/historical/xau_m5.csv
```

Grid-searches the zone/breakout levers and reports the top configurations.

### Live mode (double-gated)

1. Set `live_trading_enabled: true` in `config/config.yaml` — deliberate step #1.
2. Run `python src/main.py --mode live` — deliberate step #2.

Both are required; either one alone keeps the bot signal-only. Before every
order the executor re-checks spread, session, daily cap, cooldown, SL/TP
presence and lot validity.

## Example signal (log + journal)

```
SIGNAL BUY XAUUSD | lot 1.25 | entry 2001.85 SL 2000.30 TP 2006.50 |
retest entry after BUY breakout of zone [2000.00-2001.00] (4U/4L wick touches)
```

## Folder structure

```
accumulation_breakout_bot/
├── README.md
├── requirements.txt
├── .env.example
├── config/
│   └── config.yaml          # every tunable parameter
├── data/
│   ├── historical/          # put backtest CSVs here
│   ├── reports/             # backtest CSV/JSON reports
│   └── journals/            # daily trade/signal/rejection journals
├── src/
│   ├── main.py              # CLI + live/signal loop
│   ├── mt5_client.py        # defensive MT5 wrapper (Windows-only import)
│   ├── config_loader.py     # pydantic-validated settings
│   ├── logger.py
│   ├── models.py            # Zone / Signal / reports dataclasses
│   ├── indicators.py        # EMA, Wilder ATR, slope
│   ├── zone_detector.py     # accumulation zone rules
│   ├── breakout_strategy.py # breakout + retest state machine
│   ├── risk_manager.py      # lot sizing + hard validation
│   ├── trade_executor.py    # the only module that sends orders
│   ├── session_filter.py    # London/NY windows (broker time)
│   ├── news_filter.py       # placeholder interface, off by default
│   ├── journal.py           # CSV journal incl. rejection reasons
│   ├── backtester.py        # bar-by-bar simulation + comparison matrix
│   └── report_generator.py  # CSV/JSON/console reports
└── tests/
    ├── test_zone_detector.py
    ├── test_breakout_strategy.py
    ├── test_risk_manager.py
    └── test_backtester.py
```

## Tests

```bash
python -m pytest tests -q
```

## Notes on time

MT5 bar timestamps are **broker-server time**. The session windows in the
config are interpreted on that same clock (the bot compares bar labels, never
your PC clock), so set them to match what you see on your MT5 chart.
