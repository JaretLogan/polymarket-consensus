#!/usr/bin/env python3
"""
SEC Insider Buying -- Paper Trader
--------------------------------------
Paper-trades insider_signals.py's strategy: opens a simulated position on
each new qualifying ticker, holds it for a fixed period (default 30
calendar days -- mirrors academic post-insider-purchase drift studies),
then sells at whatever the market price is and records the result.

No brokerage account, no real money, ever. Price data comes from Stooq's
free public CSV endpoint (unofficial, no API key, but also no guarantee
of uptime/format -- see README for what to do if it breaks).
"""

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from insider_signals import build_signals, fetch_recent_filings, get_headers

STOOQ_URL = "https://stooq.com/q/d/l/"
STOOQ_HEADERS = {"User-Agent": "Mozilla/5.0 (insider-paper-trader research script)"}


def fetch_latest_close(ticker: str):
    """Most recent daily close from Stooq. Returns (date_str, close) or None
    if the ticker doesn't resolve or Stooq's format changes unexpectedly."""
    try:
        resp = requests.get(STOOQ_URL, params={"s": f"{ticker.lower()}.us", "i": "d"},
                             headers=STOOQ_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    text = resp.text.strip()
    if not text or text.lower().startswith("no data") or "Date" not in text.splitlines()[0]:
        return None

    try:
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            return None
        last = rows[-1]
        return last["Date"], float(last["Close"])
    except (KeyError, ValueError, IndexError):
        return None


# ---------- state (same shape as paper_trader.py's, so chart.py works unmodified) ----------

def load_state(path: str, starting_bankroll: float) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "starting_bankroll": starting_bankroll,
        "cash": starting_bankroll,
        "open_positions": [],
        "closed_positions": [],
        "equity_history": [],
    }


def save_state(state: dict, path: str):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ---------- core simulation steps ----------

def mark_to_market_and_settle(state: dict) -> list:
    today = datetime.now(timezone.utc).date()
    still_open, newly_closed = [], []

    for pos in state["open_positions"]:
        price_info = fetch_latest_close(pos["ticker"])
        if price_info is None:
            print(f"  [warn] couldn't refresh price for {pos['ticker']}", file=sys.stderr)
            still_open.append(pos)
            continue

        _, price = price_info
        pos["last_price"] = price
        hold_until = datetime.fromisoformat(pos["hold_until_date"]).date()

        if today >= hold_until:
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

        time.sleep(0.2)  # Stooq has no documented rate limit, but be polite anyway

    state["open_positions"] = still_open
    state["closed_positions"].extend(newly_closed)
    return newly_closed


def open_new_positions(state: dict, signals: list, stake_usd: float, max_open: int, hold_days: int) -> list:
    open_tickers = {p["ticker"] for p in state["open_positions"]}
    opened = []

    for sig in signals:
        if len(state["open_positions"]) >= max_open:
            print(f"  [info] hit max open positions ({max_open}), not opening more today")
            break
        if sig.ticker in open_tickers:
            continue  # already riding this one; a fresh signal can re-trigger after it closes
        if state["cash"] < stake_usd:
            print("  [info] out of paper cash, skipping further entries")
            break

        price_info = fetch_latest_close(sig.ticker)
        if price_info is None:
            print(f"  [warn] couldn't get a price for {sig.ticker} (ticker may not resolve on Stooq), skipping")
            continue
        date_str, price = price_info
        if price <= 0:
            continue

        shares = stake_usd / price
        hold_until = (datetime.now(timezone.utc).date() + timedelta(days=hold_days)).isoformat()
        pos = {
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "ticker": sig.ticker,
            "issuer_name": sig.issuer_name,
            "entry_price": price,
            "entry_date": date_str,
            "last_price": price,
            "hold_until_date": hold_until,
            "stake_usd": stake_usd,
            "shares": shares,
            "insider_count_at_entry": sig.insider_count,
        }
        state["open_positions"].append(pos)
        state["cash"] -= stake_usd
        open_tickers.add(sig.ticker)
        opened.append(pos)
        time.sleep(0.2)

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
        "equity": equity, "starting_bankroll": state["starting_bankroll"], "roi_pct": round(roi, 2),
        "wins": wins, "losses": losses, "pushes": pushes,
        "win_rate_pct": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "realized_pnl": realized_pnl, "open_count": len(state["open_positions"]), "closed_count": len(closed),
    }


def print_report(summary: dict, newly_closed: list, newly_opened: list, state: dict):
    print(f"\n{'='*70}\nINSIDER BUYING -- PAPER TRADING REPORT\n{'='*70}")
    print(f"Equity: ${summary['equity']:,.2f}  (started ${summary['starting_bankroll']:,.2f})  "
          f"ROI: {summary['roi_pct']:+.2f}%")
    wr = f"{summary['win_rate_pct']}%" if summary['win_rate_pct'] is not None else "n/a (no closed trades yet)"
    print(f"Record: {summary['wins']}W-{summary['losses']}L-{summary['pushes']}P   Win rate: {wr}   "
          f"Realized P&L: ${summary['realized_pnl']:,.2f}")
    print(f"Open positions: {summary['open_count']}   Closed total: {summary['closed_count']}")

    if newly_closed:
        print(f"\n-- Settled today ({len(newly_closed)}) --")
        for p in newly_closed:
            print(f"  [{p['result'].upper()}] {p['ticker']} ({p['issuer_name']})  "
                  f"entry ${p['entry_price']:.2f} -> ${p['exit_price']:.2f}  pnl ${p['pnl']:+,.2f}")

    if newly_opened:
        print(f"\n-- Opened today ({len(newly_opened)}) --")
        for p in newly_opened:
            print(f"  {p['ticker']} ({p['issuer_name']})  @ ${p['entry_price']:.2f}  "
                  f"(${p['stake_usd']:.0f}, {p['insider_count_at_entry']} insider(s), "
                  f"holds until {p['hold_until_date']})")

    if state["open_positions"]:
        print(f"\n-- Currently open ({len(state['open_positions'])}) --")
        for p in state["open_positions"]:
            cur = p.get("last_price", p["entry_price"])
            unrealized = p["shares"] * cur - p["stake_usd"]
            print(f"  {p['ticker']}  entry ${p['entry_price']:.2f} now ${cur:.2f}  "
                  f"unrealized ${unrealized:+,.2f}  (holds until {p['hold_until_date']})")


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
            lines.append(f"[{p['result'].upper()}] {p['ticker']} | pnl ${p['pnl']:+,.2f}")
    if newly_opened:
        lines.append("\n**Opened today:**")
        for p in newly_opened[:10]:
            lines.append(f"{p['ticker']} @ ${p['entry_price']:.2f} ({p['insider_count_at_entry']} insider(s))")

    embed = {
        "title": "Insider Buying -- Paper Trading Update",
        "color": 5763719 if summary["roi_pct"] >= 0 else 15548997,
        "description": "\n".join(lines)[:4000],
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    resp.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Paper-trade the SEC insider open-market buy signal.")
    ap.add_argument("--state-file", default="insider_trades.json")
    ap.add_argument("--starting-bankroll", type=float, default=1000.0)
    ap.add_argument("--stake", type=float, default=50.0, help="$ size per new paper position (default 50)")
    ap.add_argument("--max-open", type=int, default=20)
    ap.add_argument("--hold-days", type=int, default=30, help="calendar days to hold before selling (default 30)")
    ap.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"))
    ap.add_argument("--lookback-hours", type=float, default=24.0)
    ap.add_argument("--max-pages", type=int, default=15)
    ap.add_argument("--min-insiders", type=int, default=2, help="2+ requires a genuine cluster, not a lone buy")
    ap.add_argument("--min-value", type=float, default=25000.0)
    ap.add_argument("--no-new-entries", action="store_true")
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    args = ap.parse_args()

    state = load_state(args.state_file, args.starting_bankroll)

    print(f"Refreshing {len(state['open_positions'])} open position(s)...")
    newly_closed = mark_to_market_and_settle(state)

    newly_opened = []
    if not args.no_new_entries:
        headers = get_headers(args.user_agent or "")
        print(f"Pulling Form 4 filings from the last {args.lookback_hours}h...")
        filings = fetch_recent_filings(headers, args.lookback_hours, args.max_pages)
        print(f"Found {len(filings)} unique filing(s). Parsing for open-market buys...")
        signals = build_signals(headers, filings, args.min_value, args.min_insiders)
        newly_opened = open_new_positions(state, signals, args.stake, args.max_open, args.hold_days)

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
