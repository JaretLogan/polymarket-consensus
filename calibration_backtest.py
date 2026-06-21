#!/usr/bin/env python3
"""
Polymarket Calibration Bias Backtest
----------------------------------------
Tests whether Polymarket itself is systematically mispriced at the
extremes -- the "favorite-longshot bias" well documented in other
betting markets (longshots get overbought relative to how often they
actually happen; favorites get underbought). This is fundamentally
different from the consensus bot: instead of trusting other traders,
this checks whether the MARKET's own prices are internally consistent
with reality, using Polymarket's own historical resolved markets.

Pulls a large sample of CLOSED, BINARY (Yes/No) markets, looks up each
one's price some number of days before it resolved (a real ex-ante
snapshot, not the final near-certain price), and compares the AVERAGE
priced probability in each price bucket against how often that outcome
ACTUALLY happened. A real, sustained gap between the two is the edge.

Restricted to binary markets on purpose: bucketing only outcome[0] of
each Yes/No market gives one independent observation per market across
the full 0-100% range (different markets land in different buckets).
Multi-outcome markets (3+ candidates etc.) are excluded -- mixing them
in would require different statistical handling and isn't worth the
complexity for a first pass.

Output: calibration_table.json -- feed this into calibration_signals.py
to scan currently-open markets for the same mispricing pattern.

Uses Polymarket's free public Gamma + CLOB endpoints. No API key, no
auth, but be polite with request volume (this script is intended to run
weekly, not daily -- it makes one HTTP request per market sampled).
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"
HEADERS = {"User-Agent": "calibration-backtest/1.0 (personal research script)"}

# Bucket edges concentrated at the extremes, where favorite-longshot
# bias is strongest and most documented in the literature.
DEFAULT_BUCKETS = [0.0, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 0.98, 1.0]


def parse_list_field(raw):
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []


def fetch_closed_binary_markets(max_markets: int, min_volume: float, max_pages: int) -> list:
    """Paginate Gamma's closed-markets list, keep only binary (Yes/No) markets above min_volume."""
    out = []
    offset = 0
    page_size = 100
    for _ in range(max_pages):
        if len(out) >= max_markets:
            break
        params = {
            "closed": "true",
            "limit": page_size,
            "offset": offset,
            "order": "volume",
            "ascending": "false",
        }
        resp = requests.get(GAMMA_MARKETS_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        for m in batch:
            outcomes = parse_list_field(m.get("outcomes"))
            outcome_prices = parse_list_field(m.get("outcomePrices"))
            clob_ids = parse_list_field(m.get("clobTokenIds"))
            volume = float(m.get("volume") or 0)
            if len(outcomes) != 2 or len(outcome_prices) != 2 or len(clob_ids) != 2:
                continue  # only binary markets
            if volume < min_volume:
                continue
            if not m.get("endDate"):
                continue
            out.append({
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "end_date": m["endDate"],
                "outcome_prices": outcome_prices,
                "yes_token_id": clob_ids[0],
                "volume": volume,
            })

        offset += page_size
        time.sleep(0.15)

    return out[:max_markets]


def fetch_price_at_offset(token_id: str, end_date_str: str, days_before: float, tolerance_days: float = 2.0):
    """
    Look up the closest available price point to (endDate - days_before).
    Returns None if no history, or if the closest point found is further
    than tolerance_days away from the target (meaning the market didn't
    have enough lifespan/history to give a meaningful reading there).
    """
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except ValueError:
        return None

    target_dt = end_dt - timedelta(days=days_before)
    target_ts = int(target_dt.timestamp())

    params = {"market": token_id, "interval": "max", "fidelity": 720}  # 12h -- reliable for closed markets
    try:
        resp = requests.get(PRICES_HISTORY_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        history = resp.json().get("history", [])
    except (requests.RequestException, ValueError):
        return None

    if not history:
        return None

    closest = min(history, key=lambda pt: abs(pt["t"] - target_ts))
    if abs(closest["t"] - target_ts) > tolerance_days * 86400:
        return None

    return closest["p"]


def determine_outcome_won(outcome_prices: list) -> bool:
    """outcome[0] is the one we're tracking; returns True if it resolved YES (won)."""
    try:
        p0 = float(outcome_prices[0])
    except (ValueError, TypeError, IndexError):
        return None
    if p0 >= 0.99:
        return True
    if p0 <= 0.01:
        return False
    return None  # ambiguous/unresolved-looking resolution, skip


def build_calibration_table(samples: list, buckets: list) -> dict:
    bucketed = [[] for _ in range(len(buckets) - 1)]
    for s in samples:
        p = s["ref_price"]
        for i in range(len(buckets) - 1):
            if buckets[i] <= p < buckets[i + 1] or (i == len(buckets) - 2 and p == buckets[-1]):
                bucketed[i].append(s)
                break

    table = []
    for i, bucket_samples in enumerate(bucketed):
        n = len(bucket_samples)
        entry = {
            "bucket_low": buckets[i], "bucket_high": buckets[i + 1], "n": n,
            "avg_priced_prob": None, "actual_resolve_rate": None, "edge": None, "stderr": None,
        }
        if n > 0:
            avg_priced = sum(s["ref_price"] for s in bucket_samples) / n
            actual_rate = sum(1 for s in bucket_samples if s["won"]) / n
            stderr = (actual_rate * (1 - actual_rate) / n) ** 0.5 if n > 1 else None
            entry.update({
                "avg_priced_prob": round(avg_priced, 4),
                "actual_resolve_rate": round(actual_rate, 4),
                "edge": round(actual_rate - avg_priced, 4),
                "stderr": round(stderr, 4) if stderr is not None else None,
            })
        table.append(entry)
    return table


def print_report(table: list, min_bucket_n: int):
    print(f"\n{'='*100}\nCALIBRATION TABLE\n{'='*100}")
    print(f"{'bucket':>14} | {'n':>6} | {'avg priced':>10} | {'actual rate':>11} | {'edge':>8} | {'edge/SE':>8}")
    print("-" * 100)
    for b in table:
        bucket_label = f"{b['bucket_low']*100:.0f}-{b['bucket_high']*100:.0f}%"
        if b["n"] == 0:
            print(f"{bucket_label:>14} | {0:>6} | {'--':>10} | {'--':>11} | {'--':>8} | {'--':>8}")
            continue
        sig = ""
        z = None
        if b["stderr"]:
            z = b["edge"] / b["stderr"]
            sig = "  **" if abs(z) >= 2 and b["n"] >= min_bucket_n else ("   *" if abs(z) >= 1.5 and b["n"] >= min_bucket_n else "")
        z_str = f"{z:+.2f}" if z is not None else "--"
        print(f"{bucket_label:>14} | {b['n']:>6} | {b['avg_priced_prob']*100:>9.1f}% | "
              f"{b['actual_resolve_rate']*100:>10.1f}% | {b['edge']*100:>+7.2f}% | {z_str:>8}{sig}")
    print("\n** = |edge/SE| >= 2 and n >= min-bucket-n (reasonably confident this bucket is mispriced)")
    print(" * = |edge/SE| >= 1.5 and n >= min-bucket-n (suggestive, weaker confidence)")
    print("edge > 0 means this bucket's outcome happens MORE than the market priced it (underpriced, consider buying)")
    print("edge < 0 means this bucket's outcome happens LESS than the market priced it (overpriced, consider fading)")


def main():
    ap = argparse.ArgumentParser(description="Backtest Polymarket's own calibration for favorite-longshot bias.")
    ap.add_argument("--max-markets", type=int, default=1500, help="how many resolved binary markets to sample")
    ap.add_argument("--max-pages", type=int, default=60, help="safety cap on Gamma list pagination, 100/page")
    ap.add_argument("--min-volume", type=float, default=5000.0, help="ignore markets with less total volume than this")
    ap.add_argument("--days-before", type=float, default=3.0, help="how many days before resolution to sample price")
    ap.add_argument("--min-bucket-n", type=int, default=30, help="minimum sample size to trust a bucket's edge")
    ap.add_argument("--out", default="calibration_table.json")
    args = ap.parse_args()

    print(f"Pulling up to {args.max_markets} closed binary markets (min volume ${args.min_volume:,.0f})...")
    markets = fetch_closed_binary_markets(args.max_markets, args.min_volume, args.max_pages)
    print(f"Got {len(markets)} qualifying markets. Looking up price ~{args.days_before}d before resolution for each...")

    samples = []
    for i, m in enumerate(markets, start=1):
        if i % 100 == 0 or i == len(markets):
            print(f"  [{i}/{len(markets)}] processed...")
        won = determine_outcome_won(m["outcome_prices"])
        if won is None:
            continue
        ref_price = fetch_price_at_offset(m["yes_token_id"], m["end_date"], args.days_before)
        if ref_price is None:
            continue
        if ref_price <= 0.001 or ref_price >= 0.999:
            continue  # already-known outcome at reference point, not a real ex-ante reading
        samples.append({"question": m["question"], "slug": m["slug"], "ref_price": ref_price, "won": won})
        time.sleep(0.12)

    print(f"\nUsable samples (had valid price history + clear resolution): {len(samples)}")
    if len(samples) < 50:
        print("[warn] very small sample -- consider lowering --min-volume or raising --max-markets", file=sys.stderr)

    table = build_calibration_table(samples, DEFAULT_BUCKETS)
    print_report(table, args.min_bucket_n)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(samples),
        "days_before": args.days_before,
        "min_volume": args.min_volume,
        "buckets": table,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nWrote calibration table to {args.out}")


if __name__ == "__main__":
    main()
