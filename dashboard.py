#!/usr/bin/env python3
"""
Combined Strategy Dashboard
------------------------------
Reads every bot's *_trades.json state file and renders a single
self-contained dashboard.html comparing all strategies side by side:
equity curves, win rates, ROI, realized P&L, and open/closed counts.

One page to answer "which of my strategies is actually working?" instead
of checking five Discord channels. Self-contained HTML (Chart.js from CDN)
so it renders in any browser or GitHub Pages.
"""

import argparse
import json
import os
from datetime import datetime, timezone

# Each bot: (state_file, display_name, color)
BOTS = [
    ("paper_trades.json",       "Consensus (copy traders)", "#3fb950"),
    ("insider_trades.json",     "Insider buying",           "#58a6ff"),
    ("calibration_trades.json", "Calibration bias",         "#bc8cff"),
    ("weather_trades.json",     "Weather temperature",      "#39c5cf"),
    ("crypto_trades.json",      "Crypto options",           "#f0883e"),
]


def load_bot(state_file: str) -> dict | None:
    if not os.path.exists(state_file):
        return None
    try:
        with open(state_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def summarize(state: dict) -> dict:
    closed = state.get("closed_positions", [])
    open_pos = state.get("open_positions", [])
    wins = sum(1 for p in closed if p.get("result") == "win")
    losses = sum(1 for p in closed if p.get("result") == "loss")
    pushes = sum(1 for p in closed if p.get("result") == "push")
    realized = sum(p.get("pnl", 0) for p in closed)
    start = state.get("starting_bankroll", 1000.0)

    open_value = sum(p.get("shares", 0) * p.get("last_price", p.get("entry_price", 0)) for p in open_pos)
    equity = round(state.get("cash", start) + open_value, 2)
    roi = (equity - start) / start * 100 if start else 0

    # equity history collapsed to one point per date
    by_date = {}
    for pt in state.get("equity_history", []):
        by_date[pt["date"]] = pt["equity"]
    series = [{"date": d, "equity": by_date[d]} for d in sorted(by_date)]

    return {
        "equity": equity, "starting_bankroll": start, "roi_pct": round(roi, 2),
        "wins": wins, "losses": losses, "pushes": pushes,
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "realized_pnl": round(realized, 2),
        "open_count": len(open_pos), "closed_count": len(closed),
        "series": series,
    }


def build_html(bot_data: list) -> str:
    """bot_data: list of (display_name, color, summary_dict)"""
    datasets = []
    cards = []
    for name, color, s in bot_data:
        points = [{"x": pt["date"], "y": pt["equity"]} for pt in s["series"]]
        datasets.append({
            "label": name, "data": points, "borderColor": color,
            "backgroundColor": color + "22", "tension": 0.2,
            "pointRadius": 2, "borderWidth": 2, "fill": False,
        })
        wr = f"{s['win_rate']}%" if s["win_rate"] is not None else "—"
        roi_color = "#3fb950" if s["roi_pct"] >= 0 else "#f85149"
        cards.append(f"""
        <div class="card">
          <div class="card-title" style="color:{color}">{name}</div>
          <div class="big" style="color:{roi_color}">{s['roi_pct']:+.2f}%</div>
          <div class="sub">${s['equity']:,.2f} equity</div>
          <table class="stats">
            <tr><td>Record</td><td>{s['wins']}W–{s['losses']}L–{s['pushes']}P</td></tr>
            <tr><td>Win rate</td><td>{wr}</td></tr>
            <tr><td>Realized P&amp;L</td><td>${s['realized_pnl']:,.2f}</td></tr>
            <tr><td>Open / Closed</td><td>{s['open_count']} / {s['closed_count']}</td></tr>
          </table>
        </div>""")

    total_closed = sum(s["closed_count"] for _, _, s in bot_data)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Strategy Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/moment.js/2.29.4/moment.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-adapter-moment/1.0.1/chartjs-adapter-moment.min.js"></script>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:24px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; margin-bottom:28px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }}
  .card-title {{ font-size:13px; font-weight:600; margin-bottom:10px; }}
  .big {{ font-size:28px; font-weight:700; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:12px; }}
  table.stats {{ width:100%; border-collapse:collapse; font-size:13px; }}
  table.stats td {{ padding:3px 0; }}
  table.stats td:first-child {{ color:var(--muted); }}
  table.stats td:last-child {{ text-align:right; }}
  .chart-wrap {{ background:var(--panel); border:1px solid var(--border); border-radius:10px;
                 padding:20px; height:440px; }}
  .footnote {{ color:var(--muted); font-size:12px; margin-top:18px; line-height:1.5; }}
</style>
</head>
<body>
  <h1>Polymarket Strategy Dashboard</h1>
  <div class="meta">Paper trading — {total_closed} resolved trades total · generated {generated}</div>
  <div class="cards">{''.join(cards)}</div>
  <div class="chart-wrap"><canvas id="equityChart"></canvas></div>
  <div class="footnote">
    All strategies start at $1,000 in simulated capital. Equity combines cash plus
    marked-to-market open positions. These are paper trades — no real money. A strategy
    needs dozens of resolved trades before its win rate is statistically meaningful;
    early numbers are noise.
  </div>
<script>
  const datasets = {json.dumps(datasets)};
  new Chart(document.getElementById('equityChart'), {{
    type: 'line',
    data: {{ datasets }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode:'nearest', intersect:false }},
      scales: {{
        x: {{ type:'time', time:{{ unit:'day' }}, grid:{{ color:'#21262d' }},
              ticks:{{ color:'#8b949e' }} }},
        y: {{ grid:{{ color:'#21262d' }}, ticks:{{ color:'#8b949e',
              callback:v=>'$'+v.toLocaleString() }} }}
      }},
      plugins: {{
        legend: {{ labels:{{ color:'#e6edf3', usePointStyle:true, padding:16 }} }},
        tooltip: {{ callbacks:{{ label:c=>c.dataset.label+': $'+c.parsed.y.toLocaleString() }} }}
      }}
    }}
  }});
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Build a combined dashboard from all bot state files.")
    ap.add_argument("--out", default="dashboard.html")
    args = ap.parse_args()

    bot_data = []
    for state_file, name, color in BOTS:
        state = load_bot(state_file)
        if state is None:
            print(f"  [skip] {state_file} not found yet")
            continue
        bot_data.append((name, color, summarize(state)))

    if not bot_data:
        print("No bot state files found. Run at least one paper trader first.")
        return

    html = build_html(bot_data)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out} comparing {len(bot_data)} strateg(y/ies)")


if __name__ == "__main__":
    main()
