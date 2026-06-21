#!/usr/bin/env python3
"""
Calibration Bias -- Paper Trader
-------------------------------------
Paper-trades calibration_signals.py's strategy: opens a simulated
position on each new flagged mispricing, and settles it for real money
math when the underlying Polymarket market actually resolves.

Reuses the exact same settlement mechanics as the original consensus
paper_trader.py (mark_to_market_and_settle, compute_equity, summarize,
state load/save) since both bots are ultimately just "buy a side of a
real Polymarket market and wait for it to resolve" -- only how the side
gets chosen differs. No need to re-derive or duplicate that logic.
"""

import argparse
import json
import os
from datetime import datetime, timezone

import requests

from calibration_signals import fetch_open_binary_markets, find_signals, load_calibration_table
from paper_trader import (
    compute_equity,
    load_state,
    mark_to_market_and_settle,
    save_state,
    summarize,
)


def open_new_positions(state: dict, signals: list, stake_usd: float, max_open: int) -> list:
    seen = {(p["condition_id"], p["outcome"]) for p in state["open_positions"]}
    seen |= {(p["condition_id"], p["outcome"]) for p in state["closed_positions"]}

    opened = []
    for sig in signals:
        if len(state["open_positions"]) >= max_open:
            print(f"  [info] hit max open positions ({max_open}), not opening more today")
            break
        key = (sig.condition_id, sig.outcome)
        if key in seen:
            continue
        if state["cash"] < stake_usd:
            print("  [info] out of paper cash, skipping further entries")
            break
        if not (0 < sig.cur_price < 1):
            continue

        shares = stake_usd / sig.cur_price
        pos = {
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "condition_id": sig.condition_id,
            "market_slug": sig.market_slug,
            "event_slug": sig.event_slug,
            "title": sig.title,
            "outcome": sig.outcome,
            "entry_price": sig.cur_price,
            "last_price": sig.cur_price,
            "stake_usd": stake_usd,
            "shares": shares,
            "bucket_at_entry": sig.bucket_label,
            "edge_at_entry": sig.edge,
            "z_at_entry": sig.edge_z,
        }
        state["open_positions"].append(pos)
        state["cash"] -= stake_usd
        seen.add(key)
        opened.append(pos)

    return opened


def print_report(summary: dict, newly_closed: list, newly_opened: list, state: dict):
    print(f"\n{'='*70}\nCALIBRATION BIAS -- PAPER TRADING REPORT\n{'='*70}")
    print(f"Equity: ${summary['equity']:,.2f}  (started ${summary['starting_bankroll']:,.2f})  "
          f"ROI: {summary['roi_pct']:+.2f}%")
    wr = f"{summary['win_rate_pct']}%" if summary['win_rate_pct'] is not None else "n/a (no closed trades yet)"
    print(f"Record: {summary['wins']}W-{summary['losses']}L-{summary['pushes']}P   Win rate: {wr}   "
          f"Realized P&L: ${summary['realized_pnl']:,.2f}")
    print(f"Open positions: {summary['open_count']}   Closed total: {summary['closed_count']}")

    if newly_closed:
        print(f"\n-- Settled today ({len(newly_closed)}) --")
        for p in newly_closed:
            print(f"  [{p['result'].upper()}] {p['title']} -> {p['outcome']}  "
                  f"entry {p['entry_price']:.2f} -> {p['exit_price']:.2f}  pnl ${p['pnl']:+,.2f}")

    if newly_opened:
        print(f"\n-- Opened today ({len(newly_opened)}) --")
        for p in newly_opened:
            print(f"  {p['title']} -> {p['outcome']}  @ {p['entry_price']:.3f}  (${p['stake_usd']:.0f}, "
                  f"bucket {p['bucket_at_entry']}, edge {p['edge_at_entry']*100:+.1f}%, z={p['z_at_entry']:+.2f})")

    if state["open_positions"]:
        print(f"\n-- Currently open ({len(state['open_positions'])}) --")
        for p in state["open_positions"]:
            cur = p.get("last_price", p["entry_price"])
            unrealized = p["shares"] * cur - p["stake_usd"]
            print(f"  {p['title']} -> {p['outcome']}  entry {p['entry_price']:.2f} now {cur:.2f}  "
                  f"unrealized ${unrealized:+,.2f}")


def send_discord(summary: dict, newly_closed: list, newly_opened: list, webhook_url: str):
    wr = f"{summary['win_rate_pct']}%" if summary['win_rate_pct'] is not None else "n/a"
    lines = [
        f"**Equity:** ${summary['equity']:,.2f}  (ROI {summary['roi_pct']:+.2f}%)",
        f"**Record:** {summary['wins']}W-{summary['losses']}L-{summary['pushes']}P  |  Win rate: {wr}  |  "
        f"Realized P&L: ${summary['realized_pnl']:,.2f}",
        f"**Open positions:** {summary['open_count']}",
    ]
    if newly_closed:
        lines.append("\n**Settled today:**")
        for p in newly_closed[:10]:
            lines.append(f"[{p['result'].upper()}] {p['title']} | pnl ${p['pnl']:+,.2f}")
    if newly_opened:
        lines.append("\n**Opened today:**")
        for p in newly_opened[:10]:
            lines.append(f"{p['title']} -> {p['outcome']} @ {p['entry_price']:.2f} "
                         f"(bucket {p['bucket_at_entry']}, edge {p['edge_at_entry']*100:+.1f}%)")

    embed = {
        "title": "Calibration Bias -- Paper Trading Update",
        "color": 10181046 if summary["roi_pct"] >= 0 else 15548997,
        "description": "\n".join(lines)[:4000],
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    resp.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Paper-trade the calibration bias strategy.")
    ap.add_argument("--state-file", default="calibration_trades.json")
    ap.add_argument("--starting-bankroll", type=float, default=1000.0)
    ap.add_argument("--stake", type=float, default=20.0)
    ap.add_argument("--max-open", type=int, default=20)
    ap.add_argument("--table", default="calibration_table.json")
    ap.add_argument("--top", type=int, default=500, help="how many open markets to scan")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--min-volume", type=float, default=5000.0)
    ap.add_argument("--min-edge", type=float, default=0.03)
    ap.add_argument("--min-z", type=float, default=1.5)
    ap.add_argument("--min-bucket-n", type=int, default=30)
    ap.add_argument("--no-new-entries", action="store_true")
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    args = ap.parse_args()

    state = load_state(args.state_file, args.starting_bankroll)

    print(f"Refreshing {len(state['open_positions'])} open position(s)...")
    newly_closed = mark_to_market_and_settle(state)

    newly_opened = []
    if not args.no_new_entries:
        table = load_calibration_table(args.table)
        print(f"Loaded calibration table (sample size {sum(b['n'] for b in table)} historical markets).")
        markets = fetch_open_binary_markets(args.top, args.min_volume, args.max_pages)
        print(f"Scanned {len(markets)} open markets for calibration mispricings...")
        signals = find_signals(markets, table, args.min_edge, args.min_z, args.min_bucket_n)
        newly_opened = open_new_positions(state, signals, args.stake, args.max_open)

    summary = summarize(state)
    state["equity_history"].append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "equity": summary["equity"],
    })

    save_state(state, args.state_file)
    print_report(summary, newly_closed, newly_opened, state)

    if args.discord_webhook:
        send_discord(summary, newly_closed, newly_opened, args.discord_webhook)
        print("\nPosted summary to Discord.")


if __name__ == "__main__":
    main()
