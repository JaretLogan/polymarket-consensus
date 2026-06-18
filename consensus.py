#!/usr/bin/env python3
"""
Polymarket Consensus Trade Finder
----------------------------------
Pulls the top N traders off Polymarket's public leaderboard, fetches each
trader's open positions, and surfaces markets where multiple top traders
are holding the SAME side (the "consensus trade" idea).

Uses Polymarket's official public Data API (no auth, no scraping):
  https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings
  https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user

This script ONLY reads public data. It does not place trades, does not
touch your wallet/private key, and does not need an account. Wiring it up
to actually execute orders is a separate, much higher-stakes step --
intentionally not included here.
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
POSITIONS_URL = "https://data-api.polymarket.com/positions"

HEADERS = {"User-Agent": "consensus-finder/1.0 (personal research script)"}


@dataclass
class ConsensusEntry:
    condition_id: str
    title: str
    outcome: str
    event_slug: str
    market_slug: str
    end_date: str = ""
    trader_count: int = 0
    total_current_value: float = 0.0
    avg_entry_price: float = 0.0
    cur_price: float = 0.0
    traders: list = field(default_factory=list)  # list of (username, value)


def days_remaining(end_date_str: str):
    """Days from now until a market's endDate. Returns None if unparseable/missing."""
    if not end_date_str:
        return None
    try:
        cleaned = end_date_str.replace("Z", "+00:00")
        end_dt = datetime.fromisoformat(cleaned)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        delta = end_dt - datetime.now(timezone.utc)
        return delta.total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def fetch_leaderboard(limit: int, period: str, category: str, order_by: str) -> list:
    """Fetch top traders from the public leaderboard endpoint."""
    params = {
        "limit": min(limit, 50),  # API caps at 50 per request
        "timePeriod": period,
        "category": category,
        "orderBy": order_by,
        "offset": 0,
    }
    out = []
    remaining = limit
    while remaining > 0:
        params["limit"] = min(remaining, 50)
        resp = requests.get(LEADERBOARD_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        out.extend(batch)
        got = len(batch)
        remaining -= got
        params["offset"] += got
        if got < params["limit"]:
            break
        time.sleep(0.2)
    return out


def fetch_positions(wallet: str, size_threshold: float = 1.0) -> list:
    """Fetch a single trader's current open positions."""
    params = {
        "user": wallet,
        "sizeThreshold": size_threshold,
        "limit": 500,
        "sortBy": "CURRENT",
        "sortDirection": "DESC",
    }
    resp = requests.get(POSITIONS_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def build_consensus(traders: list, min_traders: int, min_position_value: float,
                     max_days: float = None) -> list:
    """
    For every (conditionId, outcome) pair, count how many of the top
    traders hold that exact side, and total how much money they have on it.

    If max_days is set, only keep trades whose market resolves within that
    many days from now (markets with no/unparseable endDate are excluded
    when this filter is active, since we can't confirm they qualify).
    """
    groups: dict = defaultdict(lambda: None)

    for t in traders:
        wallet = t["proxyWallet"]
        username = t.get("userName") or wallet[:8]
        try:
            positions = fetch_positions(wallet)
        except requests.RequestException as e:
            print(f"  [warn] could not fetch positions for {username}: {e}", file=sys.stderr)
            continue

        for p in positions:
            value = p.get("currentValue", 0) or 0
            if value < min_position_value:
                continue
            key = (p["conditionId"], p["outcome"])
            if groups[key] is None:
                groups[key] = ConsensusEntry(
                    condition_id=p["conditionId"],
                    title=p.get("title", ""),
                    outcome=p["outcome"],
                    event_slug=p.get("eventSlug", ""),
                    market_slug=p.get("slug", ""),
                    end_date=p.get("endDate", ""),
                    cur_price=p.get("curPrice", 0),
                )
            entry = groups[key]
            entry.trader_count += 1
            entry.total_current_value += value
            entry.avg_entry_price += p.get("avgPrice", 0)
            entry.traders.append((username, round(value, 2)))

        time.sleep(0.15)  # be polite to the API

    results = [g for g in groups.values() if g and g.trader_count >= min_traders]
    for r in results:
        r.avg_entry_price = round(r.avg_entry_price / r.trader_count, 4)
        r.total_current_value = round(r.total_current_value, 2)

    if max_days is not None:
        filtered = []
        for r in results:
            d = days_remaining(r.end_date)
            if d is not None and 0 <= d <= max_days:
                filtered.append(r)
        results = filtered
        # when racing against a deadline, soonest-resolving first beats trader-count first
        results.sort(key=lambda r: (days_remaining(r.end_date), -r.trader_count))
    else:
        results.sort(key=lambda r: (r.trader_count, r.total_current_value), reverse=True)

    return results


def print_report(results: list, top_n: int):
    if not results:
        print("No overlapping positions found at the current thresholds.")
        return
    print(f"\n{'='*90}\nCONSENSUS TRADES (top {top_n})\n{'='*90}")
    for r in results[:top_n]:
        d = days_remaining(r.end_date)
        when = f"resolves in {d:.1f}d" if d is not None else "resolution date unknown"
        print(f"\n[{r.trader_count} traders] {r.title}  ->  {r.outcome}  ({when})")
        print(f"  market: https://polymarket.com/event/{r.event_slug}")
        print(f"  combined position value: ${r.total_current_value:,.2f}   "
              f"avg entry price: {r.avg_entry_price:.3f}   current price: {r.cur_price:.3f}")
        names = ", ".join(f"{n} (${v:,.0f})" for n, v in r.traders[:8])
        print(f"  traders: {names}{' ...' if len(r.traders) > 8 else ''}")


def write_csv(results: list, path: str):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "trader_count", "title", "outcome", "combined_value",
            "avg_entry_price", "current_price", "end_date", "days_remaining",
            "event_url", "trader_list"
        ])
        for r in results:
            writer.writerow([
                r.trader_count, r.title, r.outcome, r.total_current_value,
                r.avg_entry_price, r.cur_price, r.end_date, days_remaining(r.end_date),
                f"https://polymarket.com/event/{r.event_slug}",
                "; ".join(f"{n}:${v:,.0f}" for n, v in r.traders),
            ])


def write_json(results: list, path: str):
    payload = [
        {
            "trader_count": r.trader_count,
            "title": r.title,
            "outcome": r.outcome,
            "combined_value": r.total_current_value,
            "avg_entry_price": r.avg_entry_price,
            "current_price": r.cur_price,
            "end_date": r.end_date,
            "days_remaining": days_remaining(r.end_date),
            "event_url": f"https://polymarket.com/event/{r.event_slug}",
            "traders": [{"username": n, "value": v} for n, v in r.traders],
        }
        for r in results
    ]
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def send_discord(results: list, webhook_url: str, top_n: int):
    """Post the top consensus trades to a Discord channel via webhook."""
    if not results:
        payload = {"content": "Polymarket consensus check ran -- no overlapping trades found today."}
        requests.post(webhook_url, json=payload, timeout=15).raise_for_status()
        return

    fields = []
    for r in results[:min(top_n, 10)]:  # Discord embeds cap at 25 fields; keep it readable
        names = ", ".join(n for n, _ in r.traders[:6])
        if len(r.traders) > 6:
            names += f" +{len(r.traders) - 6} more"
        d = days_remaining(r.end_date)
        when = f"resolves in {d:.1f}d" if d is not None else "resolution unknown"
        fields.append({
            "name": f"[{r.trader_count} traders] {r.title} -> {r.outcome}",
            "value": (f"${r.total_current_value:,.0f} combined | entry {r.avg_entry_price:.2f} -> "
                      f"now {r.cur_price:.2f} | {when}\n{names}\nhttps://polymarket.com/event/{r.event_slug}"),
            "inline": False,
        })

    embed = {
        "title": f"Polymarket Consensus Trades ({len(results)} found)",
        "color": 3447003,
        "fields": fields,
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    resp.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Find consensus trades among Polymarket's top traders.")
    ap.add_argument("--top", type=int, default=50, help="how many top traders to pull from the leaderboard (default 50)")
    ap.add_argument("--period", choices=["DAY", "WEEK", "MONTH", "ALL"], default="MONTH", help="leaderboard time window")
    ap.add_argument("--category", default="OVERALL",
                     choices=["OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE",
                              "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE"])
    ap.add_argument("--order-by", choices=["PNL", "VOL"], default="PNL", dest="order_by")
    ap.add_argument("--min-traders", type=int, default=3, help="minimum overlapping traders to count as 'consensus'")
    ap.add_argument("--min-position-value", type=float, default=25.0, help="ignore dust positions below this $ value")
    ap.add_argument("--max-days", type=float, default=None,
                     help="only show trades resolving within this many days (e.g. 3). Default: no limit")
    ap.add_argument("--show", type=int, default=15, help="how many consensus trades to print")
    ap.add_argument("--csv", help="optional path to write full results as CSV")
    ap.add_argument("--json", help="optional path to write full results as JSON")
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"),
                     help="Discord webhook URL to post results to (or set DISCORD_WEBHOOK_URL env var)")
    args = ap.parse_args()

    print(f"Fetching top {args.top} traders ({args.period}, ranked by {args.order_by}, category={args.category})...")
    traders = fetch_leaderboard(args.top, args.period, args.category, args.order_by)
    print(f"Got {len(traders)} traders. Pulling their open positions...")

    results = build_consensus(traders, args.min_traders, args.min_position_value, args.max_days)

    print_report(results, args.show)

    if args.csv:
        write_csv(results, args.csv)
        print(f"\nWrote {len(results)} rows to {args.csv}")
    if args.json:
        write_json(results, args.json)
        print(f"Wrote {len(results)} rows to {args.json}")
    if args.discord_webhook:
        send_discord(results, args.discord_webhook, args.show)
        print("Posted results to Discord.")


if __name__ == "__main__":
    main()
