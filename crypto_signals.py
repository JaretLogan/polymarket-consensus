#!/usr/bin/env python3
"""
Polymarket Crypto Options-Implied Bot -- Signal Finder
---------------------------------------------------------
Finds Polymarket crypto price markets ("Will Bitcoin be above $X on DATE?",
"Will Ethereum dip to $Y in June?", etc.), computes the option-implied
probability of that outcome from Deribit's live options data, and flags
markets where Polymarket's price differs meaningfully from what professional
options traders are pricing.

Why this has a real edge: Polymarket crypto markets are priced by a retail
crowd. Deribit is where professional crypto options flow happens -- its
implied volatility surface represents real capital from sophisticated
traders pricing the exact same "where will BTC be on date X" question.
When the two disagree, the options market is usually the sharper estimate.

Method:
  1. Pull Polymarket crypto threshold markets resolving within N days.
  2. For each, get Deribit's ATM implied volatility for the matching expiry.
  3. Use Black-Scholes to compute the risk-neutral P(spot >/< strike at expiry).
  4. Compare to Polymarket's price; flag gaps above a threshold.

Data sources (both free, no API key):
  - Polymarket markets: gamma-api.polymarket.com/events
  - Crypto options:     deribit.com/api/v2/public/* (no auth for public data)
"""

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from calibration_backtest import parse_list_field

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
DERIBIT_URL = "https://www.deribit.com/api/v2/public"
HEADERS = {"User-Agent": "crypto-options-bot/1.0 (personal research script)"}

# Map Polymarket asset keywords to Deribit currency codes
ASSET_MAP = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
}


# -----------------------------------------------------------------------
# Black-Scholes probability helpers
# -----------------------------------------------------------------------

def normal_cdf(x: float) -> float:
    return (1 + math.erf(x / math.sqrt(2))) / 2


def prob_above_strike(spot: float, strike: float, iv: float, t_years: float) -> float:
    """
    Risk-neutral probability that spot price > strike at expiry, under
    Black-Scholes (lognormal) dynamics with zero drift (crypto has no
    meaningful risk-free carry assumption for this purpose).

    P(S_T > K) = N(d2), where d2 = [ln(S/K) - 0.5*sigma^2*T] / (sigma*sqrt(T))
    """
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) - 0.5 * iv * iv * t_years) / (iv * math.sqrt(t_years))
    return normal_cdf(d2)


# -----------------------------------------------------------------------
# Deribit data
# -----------------------------------------------------------------------

_index_cache = {}


def deribit_get(method: str, params: dict):
    try:
        resp = requests.get(f"{DERIBIT_URL}/{method}", params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result")
    except (requests.RequestException, ValueError) as e:
        print(f"  [warn] Deribit {method} failed: {e}", file=sys.stderr)
        return None


def get_spot(currency: str) -> float | None:
    """Current index (spot) price for the currency."""
    if currency in _index_cache:
        return _index_cache[currency]
    result = deribit_get("get_index_price", {"index_name": f"{currency.lower()}_usd"})
    if result and "index_price" in result:
        _index_cache[currency] = result["index_price"]
        return result["index_price"]
    return None


def get_atm_iv(currency: str, target_date: datetime) -> float | None:
    """
    Get an at-the-money implied volatility (as a decimal, e.g. 0.55 = 55%)
    for the expiry closest to target_date, by scanning the option chain
    summary and picking the nearest-expiry, nearest-ATM option's mark IV.
    """
    summaries = deribit_get("get_book_summary_by_currency",
                            {"currency": currency, "kind": "option"})
    if not summaries:
        return None

    spot = get_spot(currency)
    if not spot:
        return None

    target_ts = target_date.timestamp()
    best = None  # (expiry_diff, strike_diff, mark_iv)

    for s in summaries:
        name = s.get("instrument_name", "")
        # format: BTC-27JUN25-100000-C
        m = re.match(r'^[A-Z]+-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-[CP]$', name)
        if not m:
            continue
        expiry_str, strike_str = m.group(1), m.group(2)
        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        strike = float(strike_str)
        mark_iv = s.get("mark_iv")
        if mark_iv is None or mark_iv <= 0:
            continue

        expiry_diff = abs(expiry_dt.timestamp() - target_ts)
        strike_diff = abs(strike - spot)
        candidate = (expiry_diff, strike_diff, mark_iv / 100.0)  # Deribit IV is in %, convert to decimal
        if best is None or candidate[:2] < best[:2]:
            best = candidate

    return best[2] if best else None


# -----------------------------------------------------------------------
# Polymarket market parsing
# -----------------------------------------------------------------------

def detect_asset(text: str) -> str | None:
    t = text.lower()
    for kw, code in ASSET_MAP.items():
        if kw in t:
            return code
    return None


def parse_threshold(question: str) -> tuple[float, str] | None:
    """
    Extract (strike, direction) from a crypto threshold question.
    direction is 'above' or 'below'.
    Examples:
      "Will Bitcoin reach $110,000 by June 30?"        -> (110000, 'above')
      "Will Bitcoin be above $110k on June 30?"        -> (110000, 'above')
      "Will Ethereum dip below $2,000 in June?"        -> (2000, 'below')
      "Will Bitcoin hit $120k in June?"                -> (120000, 'above')
    """
    q = question.lower()

    # find a dollar amount, allowing $110,000 / $110k / 110000
    m = re.search(r'\$?\s*([\d,]+(?:\.\d+)?)\s*(k|thousand|m|million)?', q)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    suffix = m.group(2)
    if suffix in ("k", "thousand"):
        num *= 1_000
    elif suffix in ("m", "million"):
        num *= 1_000_000
    if num < 10:  # likely matched a date or something non-price
        return None

    # direction
    if re.search(r'\b(above|over|exceed|reach|hit|surpass|more than|greater)\b', q):
        return (num, "above")
    if re.search(r'\b(below|under|dip|drop|less than|fall|lower)\b', q):
        return (num, "below")
    # "reach $X" without explicit direction defaults to 'above'
    return (num, "above")


def parse_date(text: str) -> datetime | None:
    m = re.search(r'(?:on|by|before)\s+([A-Za-z]+ \d{1,2}(?:,\s*\d{4})?)', text, re.I)
    if not m:
        return None
    for fmt in ("%B %d, %Y", "%B %d"):
        try:
            dt = datetime.strptime(m.group(1).strip(), fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# -----------------------------------------------------------------------
# Signal building
# -----------------------------------------------------------------------

@dataclass
class CryptoSignal:
    condition_id: str
    title: str
    outcome: str
    event_slug: str
    market_slug: str
    asset: str
    strike: float
    direction: str
    target_date: str
    cur_price: float
    model_prob: float
    edge: float
    spot: float
    iv: float


def fetch_crypto_markets(max_markets: int, max_days: int) -> list:
    out = []
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff = now + timedelta(days=max_days)
    offset = 0
    page_size = 100

    for _ in range(20):
        if len(out) >= max_markets:
            break
        params = {
            "closed": "false",
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": page_size, "offset": offset,
        }
        try:
            resp = requests.get(GAMMA_EVENTS_URL, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as e:
            print(f"  [warn] event fetch failed at offset {offset}: {e}", file=sys.stderr)
            break
        if not batch:
            break

        for event in batch:
            title = event.get("title", "")
            if not detect_asset(title):
                continue
            if not re.search(r'\$|\bk\b|reach|above|below|hit|dip|price', title, re.I):
                continue
            for m in event.get("markets", []):
                m["_event_title"] = title
                m["_event_slug"] = event.get("slug", "")
                out.append(m)

        if len(batch) < page_size:
            break
        offset += page_size
        time.sleep(0.12)

    print(f"  Found {len(out)} candidate crypto market outcomes resolving within {max_days}d")
    return out[:max_markets]


def build_signals(markets: list, min_edge: float) -> list:
    signals = []
    iv_cache = {}

    for m in markets:
        title = m.get("_event_title") or m.get("question", "")
        question = m.get("question", "") or title

        asset = detect_asset(title) or detect_asset(question)
        if not asset:
            continue
        threshold = parse_threshold(question) or parse_threshold(title)
        if not threshold:
            continue
        strike, direction = threshold
        target_date = parse_date(question) or parse_date(title)
        if not target_date:
            continue

        from datetime import timedelta
        t_years = max((target_date - datetime.now(timezone.utc)).total_seconds(), 0) / (365.25 * 86400)
        if t_years <= 0:
            continue

        spot = get_spot(asset)
        if not spot:
            continue

        iv_key = (asset, target_date.strftime("%Y-%m-%d"))
        if iv_key not in iv_cache:
            iv_cache[iv_key] = get_atm_iv(asset, target_date)
            time.sleep(0.15)
        iv = iv_cache[iv_key]
        if not iv:
            continue

        p_above = prob_above_strike(spot, strike, iv, t_years)
        model_prob_yes = p_above if direction == "above" else (1 - p_above)

        outcome_prices = parse_list_field(m.get("outcomePrices", "[]"))
        if len(outcome_prices) < 2:
            continue
        try:
            yes_price = float(outcome_prices[0])
            no_price = float(outcome_prices[1])
        except (ValueError, TypeError):
            continue

        yes_edge = model_prob_yes - yes_price
        no_edge = (1 - model_prob_yes) - no_price

        if abs(yes_edge) >= abs(no_edge) and abs(yes_edge) >= min_edge:
            outcome, price, mp, edge = "Yes", yes_price, model_prob_yes, yes_edge
        elif abs(no_edge) > abs(yes_edge) and abs(no_edge) >= min_edge:
            outcome, price, mp, edge = "No", no_price, 1 - model_prob_yes, no_edge
        else:
            continue

        signals.append(CryptoSignal(
            condition_id=m.get("conditionId", "") or m.get("id", ""),
            title=title, outcome=outcome,
            event_slug=m.get("_event_slug") or m.get("slug", ""),
            market_slug=m.get("slug", ""),
            asset=asset, strike=strike, direction=direction,
            target_date=target_date.strftime("%Y-%m-%d"),
            cur_price=price, model_prob=round(mp, 4), edge=round(edge, 4),
            spot=round(spot, 2), iv=round(iv, 4),
        ))

    signals.sort(key=lambda s: abs(s.edge), reverse=True)
    return signals


def print_report(signals: list, show: int):
    if not signals:
        print("\nNo crypto markets diverge from options-implied probability beyond the threshold.")
        return
    print(f"\n{'='*90}\nCRYPTO OPTIONS-IMPLIED SIGNALS  (top {show} of {len(signals)})\n{'='*90}")
    for s in signals[:show]:
        print(f"\nBUY \"{s.outcome}\" @ {s.cur_price:.3f}  --  {s.title}")
        print(f"  {s.asset} spot ${s.spot:,.0f} | strike ${s.strike:,.0f} {s.direction} | "
              f"IV {s.iv*100:.1f}% | resolves {s.target_date}")
        print(f"  options-implied prob: {s.model_prob:.3f}  vs market {s.cur_price:.3f}  edge: {s.edge:+.3f}")
        print(f"  https://polymarket.com/event/{s.event_slug}")


def send_discord(signals: list, webhook_url: str, top_n: int):
    if not signals:
        requests.post(webhook_url, json={"content": "Crypto options scan ran -- no divergences found today."},
                       timeout=15).raise_for_status()
        return
    fields = []
    for s in signals[:min(top_n, 10)]:
        fields.append({
            "name": f"BUY \"{s.outcome}\" @ {s.cur_price:.2f} -- {s.title}",
            "value": (f"{s.asset} ${s.spot:,.0f} | strike ${s.strike:,.0f} {s.direction} | IV {s.iv*100:.0f}%\n"
                      f"options-implied {s.model_prob:.2f} vs market {s.cur_price:.2f} | edge {s.edge:+.3f}\n"
                      f"https://polymarket.com/event/{s.event_slug}"),
            "inline": False,
        })
    embed = {"title": f"Crypto Options Signals ({len(signals)} found)", "color": 15844367, "fields": fields}
    requests.post(webhook_url, json={"embeds": [embed]}, timeout=15).raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Find Polymarket crypto markets mispriced vs Deribit options.")
    ap.add_argument("--max-days", type=int, default=14, help="markets resolving within this many days (crypto markets are often weekly+)")
    ap.add_argument("--max-markets", type=int, default=200)
    ap.add_argument("--min-edge", type=float, default=0.08, help="minimum |model - market| to flag (default 8pp)")
    ap.add_argument("--show", type=int, default=15)
    ap.add_argument("--json")
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    args = ap.parse_args()

    print(f"Fetching crypto markets resolving within {args.max_days} days...")
    markets = fetch_crypto_markets(args.max_markets, args.max_days)
    print("Computing options-implied probabilities from Deribit...")
    signals = build_signals(markets, args.min_edge)
    print_report(signals, args.show)

    if args.json:
        with open(args.json, "w") as f:
            json.dump([s.__dict__ for s in signals], f, indent=2)
        print(f"\nWrote {len(signals)} signals to {args.json}")
    if args.discord_webhook:
        send_discord(signals, args.discord_webhook, args.show)
        print("Posted to Discord.")


if __name__ == "__main__":
    main()
