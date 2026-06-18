#!/usr/bin/env python3
"""
Polymarket Consensus Strategy -- Paper Trader
----------------------------------------------
Runs the consensus-trade strategy from consensus.py against a simulated
("paper") account: opens fake positions on new consensus trades, checks
previously-opened positions for resolution, settles wins/losses against
the REAL market outcome, and tracks running P&L over time.

No wallet, no private key, no real money at any point. State is persisted
to a JSON file (paper_trades.json by default) so each run picks up where
the last one left off.

Data sources (both public, no auth):
  - leaderboard + positions: data-api.polymarket.com (via consensus.py)
  - market resolution + live price: gamma-api.polymarket.com/markets/slug/{slug}
"""

import argparse
import ast
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

from consensus import fetch_leaderboard, build_consensus

GAMMA_MARKET_SLUG_URL = "https://gamma-api.polymarket.com/markets/slug/{slug}"
HEADERS = {"User-Agent": "consensus-finder-paper-trader/1.0"}


# ---------- state ----------

def load_state(path: str, starting_bankroll: float) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "starting_bankroll": starting_bankroll,
        "cash": starting_bankroll,
        "open_positions": [],
        "closed_positions": [],
        "equity_history": [],  # [{date, equity}]
    }


def save_state(state: dict, path: str):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ---------- market data helpers ----------

def fetch_market(slug: str) -> dict:
    resp = requests.get(GAMMA_MARKET_SLUG_URL.format(slug=slug), headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_list_field(raw):
    """Gamma API returns 'outcomes'/'outcomePrices' as JSON-encoded strings."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ast.literal_eval(raw)


def get_outcome_price(market: dict, outcome_name: str):
    outcomes = parse_list_field(market.get("outcomes", "[]"))
    prices = parse_list_field(market.get("outcomePrices", "[]"))
    for o, p in zip(outcomes, prices):
        if o == outcome_name:
            return float(p)
    return None


# ---------- core simulation steps ----------

def mark_to_market_and_settle(state: dict) -> list:
    """Refresh every open position's price; settle any whose market has closed."""
    still_open = []
    newly_closed = []
    for pos in state["open_positions"]:
        try:
            market = fetch_market(pos["market_slug"])
        except requests.RequestException as e:
            print(f"  [warn] couldn't refresh '{pos['title']}': {e}", file=sys.stderr)
            still_open.append(pos)
            continue

        price = get_outcome_price(market, pos["outcome"])
        if price is None:
            still_open.append(pos)
            continue

        pos["last_price"] = price

        if market.get("closed"):
            payout = pos["shares"] * price
            pnl = payout - pos["stake_usd"]
            pos.update({
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "exit_price": price,
                "payout": round(payout, 2),
                "pnl": round(pnl, 2),
                "result": "win" if pnl > 1e-9 else ("loss" if pnl < -1e-9 else "push"),
            })
            state["cash"] += payout
            newly_closed.append(pos)
        else:
            still_open.append(pos)

        time.sleep(0.1)

    state["open_positions"] = still_open
    state["closed_positions"].extend(newly_closed)
    return newly_closed


def open_new_positions(state: dict, consensus_trades: list, stake_usd: float, max_open: int) -> list:
    """Open a fixed-size paper position on each new consensus trade not already taken."""
    seen = {(p["condition_id"], p["outcome"]) for p in state["open_positions"]}
    seen |= {(p["condition_id"], p["outcome"]) for p in state["closed_positions"]}

    opened = []
    for trade in consensus_trades:
        if len(state["open_positions"]) >= max_open:
            print(f"  [info] hit max open positions ({max_open}), not opening more today")
            break
        key = (trade.condition_id, trade.outcome)
        if key in seen:
            continue
        if state["cash"] < stake_usd:
            print("  [info] out of paper cash, skipping further entries")
            break
        if not (0 < trade.cur_price < 1):
            continue  # already resolved or bad data, skip

        shares = stake_usd / trade.cur_price
        pos = {
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "condition_id": trade.condition_id,
            "market_slug": trade.market_slug,
            "event_slug": trade.event_slug,
            "title": trade.title,
            "outcome": trade.outcome,
            "entry_price": trade.cur_price,
            "last_price": trade.cur_price,
            "stake_usd": stake_usd,
            "shares": shares,
            "trader_count_at_entry": trade.trader_count,
        }
        state["open_positions"].append(pos)
        state["cash"] -= stake_usd
        seen.add(key)
        opened.append(pos)

    return opened


def compute_equity(state: dict) -> float:
    open_value = sum(p["shares"] * p.get("last_price", p["entry_price"]) for p in state["open_positions"])
    return round(state["cash"] + open_value, 2)


def summarize(state: dict) -> dict:
    closed = state["closed_positions"]
    wins = sum(1 for p in closed if p.get("result") == "win")
    losses = sum(1 for p in closed if p.get("result") == "loss")
    pushes = sum(1 for p in closed if p.get("result") == "push")
    realized_pnl = round(sum(p.get("pnl", 0) for p in closed), 2)
    equity = compute_equity(state)
    roi = (equity - state["starting_bankroll"]) / state["starting_bankroll"] * 100
    return {
        "equity": equity,
        "starting_bankroll": state["starting_bankroll"],
        "roi_pct": round(roi, 2),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate_pct": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "realized_pnl": realized_pnl,
        "open_count": len(state["open_positions"]),
        "closed_count": len(closed),
    }


# ---------- reporting ----------

def print_report(summary: dict, newly_closed: list, newly_opened: list, state: dict):
    print(f"\n{'='*70}\nPAPER TRADING REPORT\n{'='*70}")
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
            print(f"  {p['title']} -> {p['outcome']}  @ {p['entry_price']:.3f}  "
                  f"(${p['stake_usd']:.0f}, {p['trader_count_at_entry']} traders agreed)")

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
            lines.append(f"[{p['result'].upper()}] {p['title']} -> {p['outcome']} | pnl ${p['pnl']:+,.2f}")
    if newly_opened:
        lines.append("\n**Opened today:**")
        for p in newly_opened[:10]:
            lines.append(f"{p['title']} -> {p['outcome']} @ {p['entry_price']:.2f} "
                         f"({p['trader_count_at_entry']} traders)")

    embed = {
        "title": "Polymarket Paper Trading Update",
        "color": 5763719 if summary["roi_pct"] >= 0 else 15548997,
        "description": "\n".join(lines)[:4000],
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]}, headers=HEADERS, timeout=15)
    resp.raise_for_status()


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Paper-trade the Polymarket consensus strategy.")
    ap.add_argument("--state-file", default="paper_trades.json")
    ap.add_argument("--starting-bankroll", type=float, default=1000.0,
                     help="virtual starting cash, only used the very first run (default 1000)")
    ap.add_argument("--stake", type=float, default=20.0, help="$ size per new paper position (default 20)")
    ap.add_argument("--max-open", type=int, default=20, help="cap on simultaneous open paper positions")
    ap.add_argument("--top", type=int, default=50, help="leaderboard traders to pull")
    ap.add_argument("--period", choices=["DAY", "WEEK", "MONTH", "ALL"], default="MONTH")
    ap.add_argument("--category", default="OVERALL",
                     choices=["OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE",
                              "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE"])
    ap.add_argument("--order-by", choices=["PNL", "VOL"], default="PNL", dest="order_by")
    ap.add_argument("--min-traders", type=int, default=3)
    ap.add_argument("--min-position-value", type=float, default=25.0)
    ap.add_argument("--no-new-entries", action="store_true", help="only settle/refresh, don't open new positions")
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    args = ap.parse_args()

    state = load_state(args.state_file, args.starting_bankroll)

    print(f"Refreshing {len(state['open_positions'])} open position(s)...")
    newly_closed = mark_to_market_and_settle(state)

    newly_opened = []
    if not args.no_new_entries:
        print(f"Fetching top {args.top} traders to look for new consensus trades...")
        traders = fetch_leaderboard(args.top, args.period, args.category, args.order_by)
        consensus_trades = build_consensus(traders, args.min_traders, args.min_position_value)
        newly_opened = open_new_positions(state, consensus_trades, args.stake, args.max_open)

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
