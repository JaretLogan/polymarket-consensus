#!/usr/bin/env python3
"""
Equity Curve Chart Generator
-------------------------------
Reads paper_trades.json (produced by paper_trader.py) and renders the
simulated account's equity over time as a PNG. Meant to be embedded
directly in the README so the repo shows a live, auto-updating
performance chart -- handy for a portfolio, not just for your own
tracking.
"""

import argparse
import json
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

BG = "#0d1117"
GRID = "#21262d"
SPINE = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"


def load_equity_series(state_path: str):
    with open(state_path) as f:
        state = json.load(f)
    history = state.get("equity_history", [])

    # collapse to one point per calendar date -- if the workflow was
    # triggered manually more than once on the same day, keep the latest
    by_date = {}
    for point in history:
        by_date[point["date"]] = point["equity"]

    dates = sorted(by_date)
    equities = [by_date[d] for d in dates]
    return state, dates, equities


def render_chart(state: dict, dates: list, equities: list, out_path: str, title: str) -> bool:
    if len(dates) < 1:
        print("No equity history yet -- nothing to chart.")
        return False

    x = [datetime.fromisoformat(d) for d in dates]
    starting = state.get("starting_bankroll", equities[0])
    roi = (equities[-1] - starting) / starting * 100
    line_color = GREEN if equities[-1] >= starting else RED

    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    if len(x) == 1:
        # single data point: show it as a dot, not an invisible zero-length line
        ax.scatter(x, equities, color=line_color, s=60, zorder=3)
    else:
        ax.plot(x, equities, color=line_color, linewidth=2.2, marker="o", markersize=4, zorder=3)
        ax.fill_between(x, equities, starting, color=line_color, alpha=0.12)

    ax.axhline(starting, color=MUTED, linestyle="--", linewidth=1, alpha=0.7)
    ax.text(x[0], starting, f" start ${starting:,.0f}", color=MUTED, fontsize=9, va="bottom")

    ax.annotate(
        f"${equities[-1]:,.2f}  ({roi:+.1f}%)",
        xy=(x[-1], equities[-1]), xytext=(10, 10), textcoords="offset points",
        color=line_color, fontsize=12, fontweight="bold",
    )

    ax.set_title(title, color=TEXT, fontsize=13, pad=16, loc="left", fontweight="bold")
    ax.tick_params(colors=MUTED, labelsize=9)
    for spine in ax.spines.values():
        spine.set_color(SPINE)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_ylabel("Equity ($)", color=MUTED, fontsize=10)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    if len(x) <= 20:
        # few enough points that one tick per actual data point looks clean;
        # the default locator tends to insert duplicate/interpolated labels here
        ax.set_xticks(x)
    else:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
    fig.autofmt_xdate()

    fig.tight_layout()
    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return True


def main():
    ap = argparse.ArgumentParser(description="Render an equity curve PNG from paper_trades.json.")
    ap.add_argument("--state-file", default="paper_trades.json")
    ap.add_argument("--out", default="equity_curve.png")
    ap.add_argument("--title", default="Polymarket Consensus Strategy — Paper Trading Equity")
    args = ap.parse_args()

    state, dates, equities = load_equity_series(args.state_file)
    ok = render_chart(state, dates, equities, args.out, args.title)
    if ok:
        print(f"Wrote {args.out} ({len(dates)} data point(s), latest equity ${equities[-1]:,.2f})")


if __name__ == "__main__":
    main()
