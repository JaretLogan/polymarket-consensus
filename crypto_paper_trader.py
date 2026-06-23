#!/usr/bin/env python3
"""
Crypto Options-Implied -- Paper Trader
-----------------------------------------
Paper-trades crypto_signals.py's strategy: opens a simulated position on
each Polymarket crypto market that diverges from Deribit's options-implied
probability, settles when the market resolves. Reuses paper_trader.py's
settlement engine (same state schema, chart.py compatible).
"""

import argparse
import json
import os
from datetime import datetime, timezone

import requests

from paper_trader import (
    compute_equity, load_state, mark_to_market_and_settle, save_state, summarize,
)
from crypto_signals import CryptoSignal, build_signals, fetch_crypto_markets


def open_new_positions(state: dict, signals: list, stake_usd: float, max_open: int) -> list:
    seen_positions = {(p["condition_id"], p["outcome"]) for p in state["open_positions"]}
    seen_positions |= {(p["condition_id"], p["outcome"]) for p in state["closed_positions"]}
    seen_markets = {p["condition_id"] for p in state["open_positions"]}
    seen_markets |= {p["condition_id"] for p in state["closed_positions"]}

    opened = []
    for sig in signals:
        if len(state["open_positions"]) >= max_open:
            print(f"  [info] hit max open positions ({max_open})")
            break
        if (sig.condition_id, sig.outcome) in seen_positions:
            continue
        if sig.condition_id in seen_markets:
            continue  # don't take both sides of the same market
        if state["cash"] < stake_usd:
            print("  [info] out of paper cash")
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
            "asset": sig.asset,
            "strike": sig.strike,
            "direction": sig.direction,
            "target_date": sig.target_date,
            "model_prob_at_entry": sig.model_prob,
            "edge_at_entry": sig.edge,
            "spot_at_entry": sig.spot,
            "iv_at_entry": sig.iv,
        }
        state["open_positions"].append(pos)
        state["cash"] -= stake_usd
        seen_positions.add((sig.condition_id, sig.outcome))
        seen_markets.add(sig.condition_id)
        opened.append(pos)

    return opened


def print_report(summary: dict, newly_closed: list, newly_opened: list, state: dict):
    print(f"\n{'='*70}\nCRYPTO OPTIONS -- PAPER TRADING REPORT\n{'='*70}")
    print(f"Equity: ${summary['equity']:,.2f}  (started ${summary['starting_bankroll']:,.2f})  "
          f"ROI: {summary['roi_pct']:+.2f}%")
    wr = f"{summary['win_rate_pct']}%" if summary['win_rate_pct'] is not None else "n/a"
    print(f"Record: {summary['wins']}W-{summary['losses']}L-{summary['pushes']}P   "
          f"Win rate: {wr}   Realized P&L: ${summary['realized_pnl']:,.2f}")
    print(f"Open: {summary['open_count']}   Closed total: {summary['closed_count']}")

    if newly_closed:
        print(f"\n-- Settled today ({len(newly_closed)}) --")
        for p in newly_closed:
            print(f"  [{p['result'].upper()}] {p['title']} -> {p['outcome']}  "
                  f"entry {p['entry_price']:.2f} -> {p['exit_price']:.2f}  pnl ${p['pnl']:+,.2f}  "
                  f"(options-implied {p.get('model_prob_at_entry', 0):.2f})")

    if newly_opened:
        print(f"\n-- Opened today ({len(newly_opened)}) --")
        for p in newly_opened:
            print(f"  {p['title']} -> {p['outcome']} @ {p['entry_price']:.3f}  "
                  f"({p['asset']} ${p['spot_at_entry']:,.0f}, strike ${p['strike']:,.0f} {p['direction']}, "
                  f"model {p['model_prob_at_entry']:.2f}, edge {p['edge_at_entry']:+.3f})")

    if state["open_positions"]:
        print(f"\n-- Currently open ({len(state['open_positions'])}) --")
        for p in state["open_positions"]:
            cur = p.get("last_price", p["entry_price"])
            unrealized = p["shares"] * cur - p["stake_usd"]
            print(f"  {p['title']} -> {p['outcome']}  entry {p['entry_price']:.2f} now {cur:.2f}  "
                  f"unrealized ${unrealized:+,.2f}  (resolves {p['target_date']})")


def send_discord(summary: dict, newly_closed: list, newly_opened: list, webhook_url: str):
    wr = f"{summary['win_rate_pct']}%" if summary['win_rate_pct'] is not None else "n/a"
    lines = [
        f"**Equity:** ${summary['equity']:,.2f}  (ROI {summary['roi_pct']:+.2f}%)",
        f"**Record:** {summary['wins']}W-{summary['losses']}L-{summary['pushes']}P  |  "
        f"Win rate: {wr}  |  Realized P&L: ${summary['realized_pnl']:,.2f}",
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
                         f"(model {p['model_prob_at_entry']:.2f}, edge {p['edge_at_entry']:+.3f})")

    embed = {
        "title": "Crypto Options -- Paper Trading Update",
        "color": 15844367 if summary["roi_pct"] >= 0 else 15548997,
        "description": "\n".join(lines)[:4000],
    }
    requests.post(webhook_url, json={"embeds": [embed]}, timeout=15).raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Paper-trade the crypto options-implied signal.")
    ap.add_argument("--state-file", default="crypto_trades.json")
    ap.add_argument("--starting-bankroll", type=float, default=1000.0)
    ap.add_argument("--stake", type=float, default=20.0)
    ap.add_argument("--max-open", type=int, default=20)
    ap.add_argument("--max-days", type=int, default=14)
    ap.add_argument("--max-markets", type=int, default=200)
    ap.add_argument("--min-edge", type=float, default=0.08)
    ap.add_argument("--no-new-entries", action="store_true")
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    args = ap.parse_args()

    state = load_state(args.state_file, args.starting_bankroll)

    print(f"Refreshing {len(state['open_positions'])} open position(s)...")
    newly_closed = mark_to_market_and_settle(state)

    newly_opened = []
    if not args.no_new_entries:
        markets = fetch_crypto_markets(args.max_markets, args.max_days)
        print("Computing options-implied probabilities...")
        signals = build_signals(markets, args.min_edge)
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
        print("\nPosted to Discord.")


if __name__ == "__main__":
    main()
