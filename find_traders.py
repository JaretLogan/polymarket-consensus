#!/usr/bin/env python3
"""
Polymarket Trader Discovery
-----------------------------
The default leaderboard pull (consensus.py) is one slice: top 50 traders,
ranked by 30-day PnL. That's a thin sample, and a single good month can be
one lucky concentrated bet rather than skill.

This script pulls EVERY time window (day/week/month/all-time) and ranks
traders by CONSISTENCY -- how many of those separate leaderboards they
show up on -- rather than just whoever has the single biggest number
right now. A trader who's top-50 on the daily, weekly, monthly, AND
all-time boards simultaneously is a much stronger "follow" candidate than
someone who spiked once.

Outputs a watchlist.json you can feed straight into consensus.py and
paper_trader.py via --watchlist, replacing the single-leaderboard pull
with this broader, better-vetted pool.
"""

import argparse
import json
import time

import requests

from consensus import fetch_leaderboard, HEADERS  # noqa: F401 (HEADERS re-exported for consistency)

PERIODS = ["DAY", "WEEK", "MONTH", "ALL"]
ALL_CATEGORIES = ["OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE",
                   "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE"]


def discover(top_per_slice: int, periods: list, categories: list, order_by: str) -> dict:
    """Pull every (period, category) combo and merge by wallet."""
    traders = {}
    total_slices = len(periods) * len(categories)
    done = 0

    for period in periods:
        for category in categories:
            done += 1
            print(f"  [{done}/{total_slices}] {period} / {category} ...")
            try:
                batch = fetch_leaderboard(top_per_slice, period, category, order_by)
            except requests.RequestException as e:
                print(f"    [warn] failed: {e}")
                continue

            key = f"{period}/{category}"
            for t in batch:
                wallet = t["proxyWallet"]
                if wallet not in traders:
                    traders[wallet] = {
                        "wallet": wallet,
                        "username": t.get("userName") or wallet[:8],
                        "verified": t.get("verifiedBadge", False),
                        "appearances": set(),
                        "pnl_by_list": {},
                        "vol_by_list": {},
                    }
                info = traders[wallet]
                info["appearances"].add(key)
                info["pnl_by_list"][key] = t.get("pnl", 0)
                info["vol_by_list"][key] = t.get("vol", 0)

            time.sleep(0.2)  # be polite to the API

    return traders


def score_and_rank(traders: dict, min_appearances: int) -> list:
    results = []
    for info in traders.values():
        n = len(info["appearances"])
        if n < min_appearances:
            continue
        all_time_key_candidates = [k for k in info["pnl_by_list"] if k.startswith("ALL/")]
        if all_time_key_candidates:
            headline_pnl = max(info["pnl_by_list"][k] for k in all_time_key_candidates)
        else:
            headline_pnl = max(info["pnl_by_list"].values())
        results.append({
            "wallet": info["wallet"],
            "username": info["username"],
            "verified": info["verified"],
            "consistency": n,
            "lists": sorted(info["appearances"]),
            "headline_pnl": round(headline_pnl, 2),
        })
    # most consistent first, biggest headline PnL as tiebreaker
    results.sort(key=lambda r: (r["consistency"], r["headline_pnl"]), reverse=True)
    return results


def print_report(results: list, show: int):
    if not results:
        print("\nNo traders met the consistency threshold. Try lowering --min-appearances.")
        return
    print(f"\n{'='*90}\nCONSISTENT WINNERS  (top {show} of {len(results)} qualifying)\n{'='*90}")
    for r in results[:show]:
        badge = "  [verified]" if r["verified"] else ""
        print(f"\n{r['username']}{badge}  --  on {r['consistency']} of the pulled leaderboards")
        print(f"  wallet: {r['wallet']}")
        print(f"  headline PnL: ${r['headline_pnl']:,.2f}")
        print(f"  appears on: {', '.join(r['lists'])}")


def write_watchlist(results: list, path: str):
    """Save in the exact shape consensus.py/paper_trader.py expect (proxyWallet + userName)."""
    payload = [{"proxyWallet": r["wallet"], "userName": r["username"]} for r in results]
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def main():
    ap = argparse.ArgumentParser(description="Find traders who rank well consistently, not just once.")
    ap.add_argument("--top-per-slice", type=int, default=50, help="traders to pull per leaderboard slice")
    ap.add_argument("--periods", nargs="+", default=PERIODS, choices=PERIODS,
                     help="which time windows to pull (default: all four)")
    ap.add_argument("--categories", nargs="+", default=["OVERALL"], choices=ALL_CATEGORIES,
                     help="which categories to pull (default: OVERALL only -- add more for a bigger, noisier pool)")
    ap.add_argument("--order-by", choices=["PNL", "VOL"], default="PNL", dest="order_by")
    ap.add_argument("--min-appearances", type=int, default=2,
                     help="keep traders who show up on at least this many pulled leaderboards (default 2)")
    ap.add_argument("--show", type=int, default=25, help="how many to print to console")
    ap.add_argument("--watchlist", default="watchlist.json",
                     help="where to save the discovered trader pool")
    args = ap.parse_args()

    print(f"Pulling {len(args.periods)} period(s) x {len(args.categories)} categor(y/ies) "
          f"= {len(args.periods) * len(args.categories)} leaderboard slices...")
    traders = discover(args.top_per_slice, args.periods, args.categories, args.order_by)
    print(f"\nFound {len(traders)} unique traders across all slices pulled.")

    results = score_and_rank(traders, args.min_appearances)
    print_report(results, args.show)

    write_watchlist(results, args.watchlist)
    print(f"\nSaved {len(results)} traders to {args.watchlist}")
    print(f"Use it: python consensus.py --watchlist {args.watchlist}")
    print(f"     or: python paper_trader.py --watchlist {args.watchlist}")


if __name__ == "__main__":
    main()
