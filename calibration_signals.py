#!/usr/bin/env python3
"""
Calibration Bias Live Signal Scanner
-----------------------------------------
Loads calibration_table.json (built by calibration_backtest.py) and scans
CURRENTLY OPEN binary markets for ones priced inside a bucket the backtest
found to be historically mispriced. Flags a BUY on whichever side
(outcome[0]/"Yes" or outcome[1]/"No") the bucket's edge favors.

This only makes sense to run after calibration_backtest.py has produced a
real table -- a freshly-initialized/empty table won't flag anything.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from calibration_backtest import parse_list_field

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
HEADERS = {"User-Agent": "calibration-signals/1.0 (personal research script)"}


@dataclass
class CalibrationSignal:
    condition_id: str
    title: str
    outcome: str          # which side to buy: "Yes" (outcome[0]) or "No" (outcome[1])
    event_slug: str
    market_slug: str
    cur_price: float       # current price of the side we're buying
    bucket_label: str
    edge: float
    edge_z: float
    bucket_n: int


def load_calibration_table(path: str) -> list:
    if not os.path.exists(path):
        print(f"[error] {path} not found. Run calibration_backtest.py first to generate it.", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    return data["buckets"]


def find_bucket(price: float, table: list):
    for b in table:
        if b["n"] == 0:
            continue
        if b["bucket_low"] <= price < b["bucket_high"] or (price == 1.0 and b["bucket_high"] == 1.0):
            return b
    return None


def fetch_open_binary_markets(max_markets: int, min_volume: float, max_pages: int) -> list:
    out = []
    offset = 0
    page_size = 100
    for _ in range(max_pages):
        if len(out) >= max_markets:
            break
        params = {
            "closed": "false", "active": "true", "limit": page_size, "offset": offset,
            "order": "volume", "ascending": "false",
        }
        try:
            resp = requests.get(GAMMA_MARKETS_URL, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as e:
            print(f"  [warn] page at offset {offset} failed ({e}), stopping pagination here "
                  f"and proceeding with {len(out)} markets collected so far", file=sys.stderr)
            break
        if not batch:
            break
        for m in batch:
            outcomes = parse_list_field(m.get("outcomes"))
            outcome_prices = parse_list_field(m.get("outcomePrices"))
            volume = float(m.get("volume") or 0)
            if len(outcomes) != 2 or len(outcome_prices) != 2:
                continue
            if volume < min_volume:
                continue
            out.append({
                "condition_id": m.get("conditionId", ""),
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "event_slug": (m.get("events") or [{}])[0].get("slug", m.get("slug", "")) if m.get("events") else m.get("slug", ""),
                "outcomes": outcomes,
                "outcome_prices": [float(p) for p in outcome_prices],
            })
        offset += page_size
        time.sleep(0.15)
    return out[:max_markets]


def find_signals(markets: list, table: list, min_edge: float, min_z: float, min_bucket_n: int) -> list:
    signals = []
    for m in markets:
        price_yes = m["outcome_prices"][0]
        if not (0.001 < price_yes < 0.999):
            continue

        bucket = find_bucket(price_yes, table)
        if bucket is None or bucket["edge"] is None or bucket["n"] < min_bucket_n:
            continue
        if bucket["stderr"]:
            z = bucket["edge"] / bucket["stderr"]
        else:
            z = 0.0
        if abs(bucket["edge"]) < min_edge or abs(z) < min_z:
            continue

        # edge > 0 means outcome[0] ("Yes") is historically underpriced at this price level -> buy Yes
        # edge < 0 means outcome[0] overpriced -> the complementary side (outcome[1], "No") is the buy
        if bucket["edge"] > 0:
            outcome_label = m["outcomes"][0]
            buy_price = price_yes
        else:
            outcome_label = m["outcomes"][1]
            buy_price = 1 - price_yes

        signals.append(CalibrationSignal(
            condition_id=m["condition_id"], title=m["question"], outcome=outcome_label,
            event_slug=m["event_slug"], market_slug=m["slug"], cur_price=buy_price,
            bucket_label=f"{bucket['bucket_low']*100:.0f}-{bucket['bucket_high']*100:.0f}%",
            edge=abs(bucket["edge"]), edge_z=z, bucket_n=bucket["n"],
        ))

    signals.sort(key=lambda s: (abs(s.edge_z), s.edge), reverse=True)
    return signals


def print_report(signals: list, show: int):
    if not signals:
        print("\nNo currently-open markets fall into a historically mispriced bucket right now.")
        return
    print(f"\n{'='*90}\nCALIBRATION SIGNALS  (top {show} of {len(signals)})\n{'='*90}")
    for s in signals[:show]:
        print(f"\nBUY \"{s.outcome}\" @ {s.cur_price:.3f}  --  {s.title}")
        print(f"  priced bucket {s.bucket_label} historically shows {s.edge*100:+.1f}% edge "
              f"(z={s.edge_z:+.2f}, n={s.bucket_n})")
        print(f"  market: https://polymarket.com/event/{s.event_slug}")


def send_discord(signals: list, webhook_url: str, top_n: int):
    if not signals:
        requests.post(webhook_url, json={"content": "Calibration scan ran -- no mispriced markets found today."},
                       timeout=15).raise_for_status()
        return
    fields = []
    for s in signals[:min(top_n, 10)]:
        fields.append({
            "name": f"BUY \"{s.outcome}\" @ {s.cur_price:.2f} -- {s.title}",
            "value": f"bucket {s.bucket_label} | historical edge {s.edge*100:+.1f}% (z={s.edge_z:+.2f}, n={s.bucket_n})\n"
                      f"https://polymarket.com/event/{s.event_slug}",
            "inline": False,
        })
    embed = {"title": f"Calibration Bias Signals ({len(signals)} found)", "color": 10181046, "fields": fields}
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    resp.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Scan open Polymarket markets for historically-mispriced price buckets.")
    ap.add_argument("--table", default="calibration_table.json")
    ap.add_argument("--top", type=int, default=500, help="how many open markets to scan")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--min-volume", type=float, default=5000.0)
    ap.add_argument("--min-edge", type=float, default=0.03, help="minimum |edge| to flag (default 3 percentage points)")
    ap.add_argument("--min-z", type=float, default=1.5, help="minimum |edge/stderr| to flag (statistical confidence)")
    ap.add_argument("--min-bucket-n", type=int, default=30)
    ap.add_argument("--show", type=int, default=15)
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    args = ap.parse_args()

    table = load_calibration_table(args.table)
    print(f"Loaded calibration table (sample size {sum(b['n'] for b in table)} historical markets).")

    print(f"Scanning up to {args.top} open binary markets...")
    markets = fetch_open_binary_markets(args.top, args.min_volume, args.max_pages)
    print(f"Got {len(markets)} qualifying open markets. Checking against calibration buckets...")

    signals = find_signals(markets, table, args.min_edge, args.min_z, args.min_bucket_n)
    print_report(signals, args.show)

    if args.discord_webhook:
        send_discord(signals, args.discord_webhook, args.show)
        print("\nPosted results to Discord.")


if __name__ == "__main__":
    main()
