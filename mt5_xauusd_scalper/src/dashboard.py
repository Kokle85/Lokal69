"""Local analytics dashboard: visual analysis of the bot's journal.

Serves a single-page dashboard (equity curve, daily P/L, win/loss, P/L by
hour/strategy/regime, score analysis, signals funnel, trades/events tables)
straight from the SQLite journal — stdlib HTTP server, no new dependencies.
Charts render with Chart.js from a CDN (needs internet in the browser).

Usage:
    python src/dashboard.py                          # live journal (data/trading_bot.db)
    python src/dashboard.py --csv data/trades.csv    # a backtest --export-trades ledger
    python src/dashboard.py --port 8888 --tz Europe/Skopje

Then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import csv as csv_mod
import json
import sqlite3
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))


# ================================================================ data layer

def _parse_ts(value: str, tz: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(tz))


def _pf(pnls: list[float]) -> float | None:
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p <= 0)
    if losses <= 0:
        return None  # rendered as em dash client-side
    return round(wins / losses, 2)


def _polarity_stats(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    return {
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "net": round(sum(pnls), 2),
        "profit_factor": _pf(pnls),
        "expectancy": round(sum(pnls) / n, 2) if n else 0.0,
    }


class TradeRow:
    """Normalized closed trade used by every aggregation."""

    __slots__ = ("closed_at", "direction", "strategy", "regime", "score",
                 "trade_no", "pnl", "close_reason", "lot", "entry", "duration")

    def __init__(self, closed_at: Optional[datetime], direction: str, strategy: str,
                 regime: str, score: Optional[int], trade_no: Optional[int],
                 pnl: float, close_reason: str, lot: float, entry: float,
                 duration: Optional[float]) -> None:
        self.closed_at = closed_at
        self.direction = direction
        self.strategy = strategy
        self.regime = regime
        self.score = score
        self.trade_no = trade_no
        self.pnl = pnl
        self.close_reason = close_reason
        self.lot = lot
        self.entry = entry
        self.duration = duration


class DashboardData:
    """Aggregations over closed trades; subclasses provide the rows."""

    def __init__(self, tz: str = "Europe/Skopje") -> None:
        self.tz = tz

    # -- to be provided by the source
    def closed_trades(self) -> list[TradeRow]:
        raise NotImplementedError

    def signals_funnel(self) -> dict[str, int]:
        return {}

    def daily_stats_rows(self) -> list[dict[str, Any]]:
        return []

    def recent_events(self, limit: int = 40) -> list[dict[str, Any]]:
        return []

    def source_label(self) -> str:
        return "unknown"

    # -- aggregations (shared)
    def summary(self) -> dict[str, Any]:
        trades = self.closed_trades()
        out = _polarity_stats([t.pnl for t in trades])
        durations = [t.duration for t in trades if t.duration is not None]
        out["avg_duration_min"] = round(sum(durations) / len(durations), 1) if durations else None
        out["source"] = self.source_label()
        today = datetime.now(ZoneInfo(self.tz)).date()
        today_pnls = [t.pnl for t in trades if t.closed_at and t.closed_at.date() == today]
        out["today_pnl"] = round(sum(today_pnls), 2)
        out["today_trades"] = len(today_pnls)
        for row in self.daily_stats_rows():
            if row["date"] == today.isoformat():
                out["daily_locked"] = bool(row["daily_locked"])
                out["today_pnl"] = row["realized_pnl"]
        return out

    def equity_curve(self) -> list[dict[str, Any]]:
        trades = sorted(
            (t for t in self.closed_trades() if t.closed_at is not None),
            key=lambda t: t.closed_at,
        )
        equity = 0.0
        points = []
        for i, t in enumerate(trades, 1):
            equity += t.pnl
            points.append({"n": i, "time": t.closed_at.isoformat(), "pnl": round(t.pnl, 2),
                           "equity": round(equity, 2)})
        return points

    def daily_pnl(self) -> list[dict[str, Any]]:
        stats = self.daily_stats_rows()
        if stats:
            return [{"date": r["date"], "pnl": round(r["realized_pnl"], 2),
                     "locked": bool(r["daily_locked"]), "target_hit": bool(r["daily_target_hit"]),
                     "max_loss_hit": bool(r["daily_max_loss_hit"])} for r in stats]
        by_day: dict[str, float] = {}
        for t in self.closed_trades():
            if t.closed_at:
                key = t.closed_at.date().isoformat()
                by_day[key] = by_day.get(key, 0.0) + t.pnl
        return [{"date": d, "pnl": round(p, 2), "locked": False, "target_hit": False,
                 "max_loss_hit": False} for d, p in sorted(by_day.items())]

    def _group(self, key_fn) -> list[dict[str, Any]]:
        groups: dict[Any, list[float]] = {}
        for t in self.closed_trades():
            k = key_fn(t)
            if k is None:
                continue
            groups.setdefault(k, []).append(t.pnl)
        return [{"key": k, **_polarity_stats(v)} for k, v in sorted(groups.items())]

    def pnl_by_hour(self) -> list[dict[str, Any]]:
        return self._group(lambda t: t.closed_at.hour if t.closed_at else None)

    def pnl_by_strategy(self) -> list[dict[str, Any]]:
        return self._group(lambda t: t.strategy or None)

    def pnl_by_regime(self) -> list[dict[str, Any]]:
        return self._group(lambda t: t.regime or None)

    def pnl_by_score(self) -> list[dict[str, Any]]:
        return self._group(lambda t: t.score)

    def pnl_by_trade_no(self) -> list[dict[str, Any]]:
        return self._group(lambda t: t.trade_no)

    def recent_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        trades = sorted(
            (t for t in self.closed_trades() if t.closed_at is not None),
            key=lambda t: t.closed_at, reverse=True,
        )[:limit]
        return [
            {"time": t.closed_at.strftime("%Y-%m-%d %H:%M"), "direction": t.direction,
             "strategy": t.strategy, "regime": t.regime, "score": t.score,
             "trade_no": t.trade_no, "lot": t.lot, "entry": t.entry,
             "close_reason": t.close_reason, "duration_min": t.duration,
             "pnl": round(t.pnl, 2)}
            for t in trades
        ]

    def payload(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "equity": self.equity_curve(),
            "daily": self.daily_pnl(),
            "by_hour": self.pnl_by_hour(),
            "by_strategy": self.pnl_by_strategy(),
            "by_regime": self.pnl_by_regime(),
            "by_score": self.pnl_by_score(),
            "by_trade_no": self.pnl_by_trade_no(),
            "funnel": self.signals_funnel(),
            "trades": self.recent_trades(),
            "events": self.recent_events(),
            "generated_at": datetime.now(ZoneInfo(self.tz)).strftime("%H:%M:%S"),
        }


class SqliteData(DashboardData):
    """Reads the live journal (data/trading_bot.db). A fresh read-only
    connection per call keeps this safe alongside the running bot."""

    def __init__(self, db_path: str | Path, tz: str = "Europe/Skopje") -> None:
        super().__init__(tz)
        self.db_path = Path(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def source_label(self) -> str:
        return f"live journal: {self.db_path}"

    def closed_trades(self) -> list[TradeRow]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT t.closed_at, t.direction, t.strategy, t.profit, t.close_reason,
                          t.lot, t.entry, t.duration_minutes, s.regime, s.score
                   FROM trades t LEFT JOIN signals s ON s.id = t.signal_id
                   WHERE t.status = 'CLOSED' AND t.profit IS NOT NULL
                   ORDER BY t.closed_at"""
            ).fetchall()
        return [
            TradeRow(
                closed_at=_parse_ts(r["closed_at"], self.tz), direction=r["direction"],
                strategy=r["strategy"], regime=r["regime"] or "", score=r["score"],
                trade_no=None, pnl=float(r["profit"]), close_reason=r["close_reason"] or "",
                lot=r["lot"], entry=r["entry"], duration=r["duration_minutes"],
            )
            for r in rows
        ]

    def signals_funnel(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT status, COUNT(*) n FROM signals GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def daily_stats_rows(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM daily_stats ORDER BY date").fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT created_at, level, event_type, message FROM bot_events "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        out = []
        for r in rows:
            ts = _parse_ts(r["created_at"], self.tz)
            out.append({"time": ts.strftime("%m-%d %H:%M") if ts else "", "level": r["level"],
                        "type": r["event_type"], "message": r["message"]})
        return out


class CsvData(DashboardData):
    """Reads a backtest --export-trades ledger (no signals/events tables)."""

    def __init__(self, csv_path: str | Path, tz: str = "Europe/Skopje") -> None:
        super().__init__(tz)
        self.csv_path = Path(csv_path)
        self._rows = self._load()

    def source_label(self) -> str:
        return f"backtest ledger: {self.csv_path}"

    def _load(self) -> list[TradeRow]:
        out: list[TradeRow] = []
        with self.csv_path.open() as fh:
            for r in csv_mod.DictReader(fh):
                out.append(
                    TradeRow(
                        closed_at=_parse_ts(r.get("close_time", ""), self.tz),
                        direction=r.get("direction", ""), strategy=r.get("strategy", ""),
                        regime=r.get("regime", ""),
                        score=int(r["score"]) if r.get("score") else None,
                        trade_no=int(r["trade_no"]) if r.get("trade_no") else None,
                        pnl=float(r.get("pnl", 0) or 0),
                        close_reason=r.get("close_reason", ""),
                        lot=float(r.get("lot", 0) or 0),
                        entry=float(r.get("entry", 0) or 0), duration=None,
                    )
                )
        return out

    def closed_trades(self) -> list[TradeRow]:
        return self._rows


# ================================================================ web layer

INDEX_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>XAUUSD Scalper — Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --border:rgba(11,11,11,.10); --blue:#2a78d6;
  --good:#0ca30c; --crit:#d03b3b; --goodtext:#006300;
  --ord1:#86b6ef; --ord2:#2a78d6; --ord3:#104281;
}
@media (prefers-color-scheme: dark){:root{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --border:rgba(255,255,255,.10); --blue:#3987e5;
  --good:#0ca30c; --crit:#d03b3b; --goodtext:#0ca30c;
  --ord1:#86b6ef; --ord2:#3987e5; --ord3:#184f95;
}}
*{box-sizing:border-box;margin:0}
body{background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:20px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:19px;font-weight:650}
.pill{font-size:12px;color:var(--ink2);border:1px solid var(--border);border-radius:999px;padding:3px 10px;background:var(--surface)}
.pill.lock{color:var(--crit);font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tile .l{font-size:12px;color:var(--muted)}
.tile .v{font-size:24px;font-weight:650;margin-top:2px}
.tile .v.pos{color:var(--goodtext)}.tile .v.neg{color:var(--crit)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px}
.card h2{font-size:13px;font-weight:600;color:var(--ink2);margin-bottom:10px}
.card .empty{color:var(--muted);font-size:13px;padding:24px 0;text-align:center}
canvas{max-height:260px}
.wide{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--muted);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--grid)}
td{padding:6px 8px;border-bottom:1px solid var(--grid);font-variant-numeric:tabular-nums}
td.pos{color:var(--goodtext);font-weight:600}td.neg{color:var(--crit);font-weight:600}
.ev{font-size:12px;color:var(--ink2);padding:4px 0;border-bottom:1px solid var(--grid)}
.ev b{color:var(--muted);font-weight:500;margin-right:6px}
#cdn-warn{display:none;color:var(--crit);margin-bottom:10px}
</style></head><body>
<header>
  <h1>XAUUSD Scalper — Analytics</h1>
  <span class="pill" id="src">…</span>
  <span class="pill" id="refresh">…</span>
  <span class="pill lock" id="lock" style="display:none">DAY LOCKED</span>
</header>
<div id="cdn-warn">Chart.js not loaded (no internet?) — tables below still work.</div>
<div class="kpis" id="kpis"></div>
<div class="grid">
  <div class="card wide"><h2>Equity curve (cumulative realized P/L, USD)</h2><canvas id="c-equity"></canvas></div>
  <div class="card"><h2>Daily P/L (USD)</h2><canvas id="c-daily"></canvas></div>
  <div class="card"><h2>Wins vs losses</h2><canvas id="c-winloss"></canvas></div>
  <div class="card"><h2>Net P/L by hour of day (USD)</h2><canvas id="c-hour"></canvas></div>
  <div class="card"><h2>Net P/L by strategy (USD)</h2><canvas id="c-strategy"></canvas></div>
  <div class="card"><h2>Net P/L by regime (USD)</h2><canvas id="c-regime"></canvas></div>
  <div class="card"><h2>Net P/L by signal score (USD)</h2><canvas id="c-score"></canvas></div>
  <div class="card"><h2>Signals funnel</h2><canvas id="c-funnel"></canvas></div>
  <div class="card"><h2>Trade 1 vs trade 2 of day (USD)</h2><canvas id="c-tradeno"></canvas></div>
  <div class="card wide"><h2>Recent trades</h2><div id="t-trades" style="overflow-x:auto"></div></div>
  <div class="card wide"><h2>Recent bot events</h2><div id="t-events"></div></div>
</div>
<script>
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const charts = {};
const HAS_CHART = typeof Chart !== 'undefined';
if(!HAS_CHART) document.getElementById('cdn-warn').style.display='block';
if(HAS_CHART){
  Chart.defaults.color = css('--muted');
  Chart.defaults.borderColor = css('--grid');
  Chart.defaults.font.family = 'system-ui,-apple-system,"Segoe UI",sans-serif';
}
const fmt = v => (v>=0?'+':'') + Number(v).toLocaleString('en-US',{maximumFractionDigits:2});
const pol = v => v >= 0 ? css('--good') : css('--crit');

function upsert(id, cfg){
  if(!HAS_CHART) return;
  if(charts[id]){ charts[id].data = cfg.data; charts[id].update('none'); return; }
  charts[id] = new Chart(document.getElementById(id), cfg);
}
const baseOpts = extra => Object.assign({
  responsive:true, maintainAspectRatio:false, animation:false,
  plugins:{ legend:{display:false},
    tooltip:{backgroundColor:css('--surface'), titleColor:css('--ink'),
             bodyColor:css('--ink2'), borderColor:css('--border'), borderWidth:1} },
  scales:{ x:{grid:{display:false}}, y:{grid:{color:css('--grid')}, border:{display:false}} }
}, extra||{});
const bar = (labels, values) => ({ type:'bar',
  data:{labels, datasets:[{data:values, backgroundColor:values.map(pol),
        borderRadius:4, borderSkipped:'start', maxBarThickness:30}]},
  options: baseOpts() });

function kpi(l, v, colored){
  const cls = colored ? (v>=0?'v pos':'v neg') : 'v';
  const text = colored ? '$'+fmt(v) : v;
  return `<div class="tile"><div class="l">${l}</div><div class="${cls}">${text}</div></div>`;
}

async function refresh(){
  let d;
  try { d = await (await fetch('/api/data')).json(); }
  catch(e){ document.getElementById('refresh').textContent='fetch failed'; return; }
  const s = d.summary;
  document.getElementById('src').textContent = s.source;
  document.getElementById('refresh').textContent = 'updated ' + d.generated_at;
  document.getElementById('lock').style.display = s.daily_locked ? '' : 'none';
  document.getElementById('kpis').innerHTML =
    kpi('Net P/L', s.net, true) + kpi('Today', s.today_pnl, true) +
    kpi('Trades', s.trades) + kpi('Win rate', s.win_rate + '%') +
    kpi('Profit factor', s.profit_factor ?? '—') + kpi('Expectancy / trade', s.expectancy, true);

  upsert('c-equity', { type:'line',
    data:{ labels: d.equity.map(p=>p.n),
      datasets:[{ label:'Equity', data:d.equity.map(p=>p.equity), borderColor:css('--blue'),
        borderWidth:2, pointRadius:0, pointHoverRadius:4, tension:0, fill:false }]},
    options: baseOpts() });

  upsert('c-daily', bar(d.daily.map(r=>r.date.slice(5)), d.daily.map(r=>r.pnl)));
  upsert('c-hour', bar(d.by_hour.map(r=>String(r.key).padStart(2,'0')+':00'), d.by_hour.map(r=>r.net)));
  upsert('c-strategy', bar(d.by_strategy.map(r=>r.key.replace('_SCALP','')), d.by_strategy.map(r=>r.net)));
  upsert('c-regime', bar(d.by_regime.map(r=>r.key), d.by_regime.map(r=>r.net)));
  upsert('c-score', bar(d.by_score.map(r=>r.key+'/13'), d.by_score.map(r=>r.net)));
  upsert('c-tradeno', bar(d.by_trade_no.map(r=>'trade '+r.key), d.by_trade_no.map(r=>r.net)));

  upsert('c-winloss', { type:'doughnut',
    data:{ labels:['Wins','Losses'],
      datasets:[{ data:[s.wins, s.losses], backgroundColor:[css('--good'), css('--crit')],
                  borderColor:css('--surface'), borderWidth:2 }]},
    options:{ responsive:true, maintainAspectRatio:false, animation:false, cutout:'62%',
      plugins:{ legend:{display:true, position:'bottom', labels:{color:css('--ink2')}} } } });

  const order = ['SENT','APPROVED','EXECUTED','REJECTED','EXPIRED','CANCELLED','NEW'];
  const stageColor = k => k==='SENT'?css('--ord1'):k==='APPROVED'?css('--ord2')
                        :k==='EXECUTED'?css('--ord3'):css('--muted');
  const fk = order.filter(k=>k in d.funnel);
  upsert('c-funnel', { type:'bar',
    data:{ labels:fk, datasets:[{ data:fk.map(k=>d.funnel[k]),
      backgroundColor:fk.map(stageColor), borderRadius:4, borderSkipped:'start',
      maxBarThickness:26 }]},
    options: baseOpts({ indexAxis:'y',
      scales:{ x:{grid:{color:css('--grid')}, border:{display:false}}, y:{grid:{display:false}} } }) });

  document.getElementById('t-trades').innerHTML = d.trades.length ?
    '<table><tr><th>Closed</th><th>Dir</th><th>Strategy</th><th>Regime</th><th>Score</th>'+
    '<th>Lot</th><th>Entry</th><th>Reason</th><th>P/L</th></tr>' +
    d.trades.map(t=>`<tr><td>${t.time}</td><td>${t.direction}</td><td>${t.strategy}</td>
      <td>${t.regime||''}</td><td>${t.score??''}</td><td>${t.lot}</td><td>${t.entry}</td>
      <td>${t.close_reason}</td><td class="${t.pnl>=0?'pos':'neg'}">$${fmt(t.pnl)}</td></tr>`).join('')
    + '</table>' : '<div class="empty">No closed trades yet.</div>';

  document.getElementById('t-events').innerHTML = d.events.length ?
    d.events.map(e=>`<div class="ev"><b>${e.time} ${e.level} ${e.type}</b>${e.message}</div>`).join('')
    : '<div class="empty">No events (backtest ledger has no event log).</div>';
}
refresh();
setInterval(refresh, 10000);
</script></body></html>
"""


def make_handler(data: DashboardData):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if self.path.startswith("/api/data"):
                try:
                    body = json.dumps(data.payload()).encode()
                    self._send(200, "application/json", body)
                except Exception as exc:  # noqa: BLE001 - report, don't kill the server
                    logger.error("Dashboard data error: {}", exc)
                    self._send(500, "application/json",
                               json.dumps({"error": str(exc)}).encode())
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode())
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            pass  # quiet; loguru handles app logging

    return Handler


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="XAUUSD scalper analytics dashboard")
    parser.add_argument("--db", default="data/trading_bot.db")
    parser.add_argument("--csv", default=None, help="backtest --export-trades ledger instead of the live DB")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tz", default="Europe/Skopje")
    args = parser.parse_args()

    if args.csv:
        source: DashboardData = CsvData(args.csv, args.tz)
    else:
        if not Path(args.db).exists():
            logger.error("Journal not found: {} — run the bot first, or pass --csv <ledger>", args.db)
            return 2
        source = SqliteData(args.db, args.tz)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(source))
    logger.info("Dashboard on http://{}:{}  ({})", args.host, args.port, source.source_label())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
