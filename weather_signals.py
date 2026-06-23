#!/usr/bin/env python3
"""
Polymarket Weather Temperature Bot -- Signal Finder
------------------------------------------------------
Finds daily temperature markets on Polymarket (resolving in 0-3 days),
pulls Open-Meteo forecast data for the EXACT weather station each market
resolves against (not city-centre coordinates -- see STATION_TABLE below),
and flags buckets where the model's implied probability differs meaningfully
from what Polymarket has priced.

The #1 cause of silent losses in Polymarket weather markets is using the
wrong coordinates. Retail traders pull "Paris" from a weather app and get
the city-centre reading; Polymarket settles Paris on Le Bourget airport,
which runs 1-3C cooler. Every city in STATION_TABLE uses the verified
resolution station from Polymarket's published market rules.

Data sources (both free, no API key):
  - Polymarket markets:   gamma-api.polymarket.com/markets
  - Weather forecast:     api.open-meteo.com/v1/forecast (Open-Meteo)

Edge calculation:
  Treat the model's forecast high/low as a Gaussian with sigma determined
  by forecast horizon (further out = wider uncertainty), then compute the
  probability mass inside each market's temperature bucket using the normal
  CDF. Compare to Polymarket's current price. Flag where |model - market| > threshold.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
from calibration_backtest import parse_list_field

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HEADERS = {"User-Agent": "weather-temperature-bot/1.0 (personal research script)"}

# -----------------------------------------------------------------------
# STATION TABLE
# Coordinates of the EXACT weather station each city's markets resolve on.
# Source: polymarketweather.com/blog/polymarket-weather-resolution-stations
# and Polymarket's published market rules (mid-2026).
#
# For cities with multiple possible stations (London, LA, Tokyo, Shanghai,
# Seoul, Taipei), both are listed -- the bot matches on ICAO code extracted
# from the market's resolution rules text where possible, otherwise uses
# the primary entry (index 0).
# -----------------------------------------------------------------------
STATION_TABLE = {
    # city keyword (lowercase, partial match) -> list of stations
    "new york":     [{"name": "LaGuardia",   "icao": "KLGA", "lat": 40.7769, "lon": -73.8740, "unit": "fahrenheit"}],
    "nyc":          [{"name": "LaGuardia",   "icao": "KLGA", "lat": 40.7769, "lon": -73.8740, "unit": "fahrenheit"}],
    "los angeles":  [{"name": "LAX",         "icao": "KLAX", "lat": 33.9425, "lon": -118.4081, "unit": "fahrenheit"},
                     {"name": "Burbank",     "icao": "KBUR", "lat": 34.2007, "lon": -118.3587, "unit": "fahrenheit"}],
    "london":       [{"name": "London City", "icao": "EGLC", "lat": 51.5053, "lon":   0.0553, "unit": "celsius"},
                     {"name": "Heathrow",    "icao": "EGLL", "lat": 51.4775, "lon":  -0.4614, "unit": "celsius"}],
    "paris":        [{"name": "Le Bourget",  "icao": "LFPB", "lat": 48.9694, "lon":   2.4414, "unit": "celsius"}],
    "tokyo":        [{"name": "Haneda",      "icao": "RJTT", "lat": 35.5494, "lon": 139.7798, "unit": "celsius"},
                     {"name": "Narita",      "icao": "RJAA", "lat": 35.7720, "lon": 140.3929, "unit": "celsius"}],
    "shanghai":     [{"name": "Pudong",      "icao": "ZSPD", "lat": 31.1443, "lon": 121.8083, "unit": "celsius"},
                     {"name": "Hongqiao",    "icao": "ZSSS", "lat": 31.1981, "lon": 121.3362, "unit": "celsius"}],
    "beijing":      [{"name": "Capital Intl","icao": "ZBAA", "lat": 40.0799, "lon": 116.5858, "unit": "celsius"}],
    "hong kong":    [{"name": "VHHH",        "icao": "VHHH", "lat": 22.3080, "lon": 113.9185, "unit": "celsius"}],
    "seoul":        [{"name": "Incheon",     "icao": "RKSI", "lat": 37.4600, "lon": 126.4407, "unit": "celsius"}],
    "taipei":       [{"name": "Taoyuan",     "icao": "RCTP", "lat": 25.0777, "lon": 121.2326, "unit": "celsius"}],
    "wuhan":        [{"name": "Tianhe",      "icao": "ZHHH", "lat": 30.7838, "lon": 114.2080, "unit": "celsius"}],
    "chicago":      [{"name": "O'Hare",      "icao": "KORD", "lat": 41.9742, "lon":  -87.9073, "unit": "fahrenheit"}],
    "miami":        [{"name": "Miami Intl",  "icao": "KMIA", "lat": 25.7959, "lon":  -80.2870, "unit": "fahrenheit"}],
    "austin":       [{"name": "Austin-Bergstrom","icao":"KAUS","lat":30.1975, "lon":  -97.6664, "unit": "fahrenheit"}],
    "seattle":      [{"name": "Sea-Tac",     "icao": "KSEA", "lat": 47.4502, "lon": -122.3088, "unit": "fahrenheit"}],
    "sydney":       [{"name": "Sydney Intl", "icao": "YSSY", "lat":-33.9399, "lon": 151.1753, "unit": "celsius"}],
    "singapore":    [{"name": "Changi",      "icao": "WSSS", "lat":  1.3644, "lon": 103.9915, "unit": "celsius"}],
    "frankfurt":    [{"name": "Frankfurt",   "icao": "EDDF", "lat": 50.0333, "lon":   8.5706, "unit": "celsius"}],
    "amsterdam":    [{"name": "Schiphol",    "icao": "EHAM", "lat": 52.3086, "lon":   4.7639, "unit": "celsius"}],
    "dubai":        [{"name": "Dubai Intl",  "icao": "OMDB", "lat": 25.2532, "lon":  55.3657, "unit": "celsius"}],
    "toronto":      [{"name": "Pearson",     "icao": "CYYZ", "lat": 43.6777, "lon":  -79.6248, "unit": "celsius"}],
}

# Sigma (forecast uncertainty in degrees) by days ahead
# Calibrated roughly from NWS/ECMWF verification stats
SIGMA_BY_DAYS = {0: 0.8, 1: 1.2, 2: 2.0, 3: 2.8, 4: 3.5, 5: 4.2}


def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution."""
    return (1 + math.erf(x / math.sqrt(2))) / 2


def bucket_probability(forecast_temp: float, sigma: float, low: float, high: float) -> float:
    """P(low <= T <= high) under N(forecast_temp, sigma)."""
    if sigma <= 0:
        return 1.0 if low <= forecast_temp <= high else 0.0
    return normal_cdf((high - forecast_temp) / sigma) - normal_cdf((low - forecast_temp) / sigma)


# -----------------------------------------------------------------------
# Parsing helpers
# -----------------------------------------------------------------------

def parse_city(title: str) -> str | None:
    """Extract city name from 'Highest/Lowest temperature in CITY on DATE?'"""
    m = re.search(r'(?:highest|lowest)\s+temperature\s+in\s+(.+?)\s+on\s+', title, re.I)
    if m:
        return m.group(1).strip()
    return None


def parse_date(title: str) -> datetime | None:
    """Extract the target date from the market title. Returns UTC date."""
    m = re.search(r'on\s+([A-Za-z]+ \d{1,2}(?:,\s*\d{4})?)\?', title, re.I)
    if not m:
        return None
    date_str = m.group(1).strip()
    for fmt in ("%B %d, %Y", "%B %d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.year == 1900:  # no year in pattern
                dt = dt.replace(year=datetime.now().year)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def is_high_temp(title: str) -> bool:
    return bool(re.search(r'highest', title, re.I))


def lookup_station(city: str, resolution_text: str = "") -> dict | None:
    """Find the station entry for a city, using ICAO hint from resolution text if available."""
    city_lower = city.lower()
    stations = None
    for key, entries in STATION_TABLE.items():
        if key in city_lower or city_lower in key:
            stations = entries
            break
    if not stations:
        return None
    if len(stations) == 1:
        return stations[0]
    # Try to match ICAO code mentioned in resolution rules
    for entry in stations:
        if entry["icao"].lower() in resolution_text.lower():
            return entry
    return stations[0]  # fall back to primary


def parse_outcome_bucket(outcome: str, unit: str) -> tuple[float, float] | None:
    """
    Parse a temperature outcome string into (low, high) inclusive bounds.
    Examples: "27°C", "68°F", "68-69°F", "Above 100°F", "Below 60°F",
              "27", "68-69", "<60", ">100"
    Returns (low, high) in the same unit as the station -- caller converts
    if needed. Returns None if unparseable.
    """
    s = outcome.strip().replace("°F", "").replace("°C", "").replace("°", "").strip()

    # "Above X" / ">X"
    m = re.match(r'^(?:above|>)\s*([\d.]+)$', s, re.I)
    if m:
        return (float(m.group(1)), float("inf"))

    # "Below X" / "<X"
    m = re.match(r'^(?:below|<)\s*([\d.]+)$', s, re.I)
    if m:
        return (float("-inf"), float(m.group(1)))

    # "X-Y" range
    m = re.match(r'^([\d.]+)\s*[-–]\s*([\d.]+)$', s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi))

    # Single value "X" -> bucket [X-0.5, X+0.5)
    m = re.match(r'^([\d.]+)$', s)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5)

    return None


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


# -----------------------------------------------------------------------
# API calls
# -----------------------------------------------------------------------

def fetch_weather_markets(max_markets: int, max_days: int) -> list:
    """
    Pull active temperature markets resolving within max_days.

    Key fix: filter by end_date_max rather than paginating through ALL open
    markets sorted by volume. Temperature markets only do $5-50K volume each
    (they resolve daily) so they were buried past offset 2100 in a volume-sorted
    list -- right where Gamma API throws a 422. Filtering by end date gives
    a tiny, targeted result set that is almost entirely temperature markets.
    """
    out = []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=max_days)
    total_raw = 0
    offset = 0
    page_size = 100

    for _ in range(20):
        if len(out) >= max_markets:
            break
        params = {
            "closed": "false",
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": page_size,
            "offset": offset,
        }
        try:
            resp = requests.get(GAMMA_MARKETS_URL, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as e:
            print(f"  [warn] market fetch failed at offset {offset}: {e}", file=sys.stderr)
            break
        if not batch:
            break

        total_raw += len(batch)
        for m in batch:
            title = m.get("question", "")
            if not re.search(r'(highest|lowest)\s+temperature', title, re.I):
                continue
            out.append(m)

        if len(batch) < page_size:
            break
        offset += page_size
        time.sleep(0.12)

    print(f"  Scanned {total_raw} short-horizon markets, found {len(out)} temperature markets resolving within {max_days}d")
    return out[:max_markets]



def fetch_forecast_temp(lat: float, lon: float, target_date: datetime, unit: str) -> float | None:
    """Fetch daily max or min temp forecast from Open-Meteo for the given station coords."""
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": unit,
        "timezone": "UTC",
        "forecast_days": 7,
    }
    try:
        resp = requests.get(OPEN_METEO_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    target_str = target_date.strftime("%Y-%m-%d")
    for i, d in enumerate(dates):
        if d == target_str:
            return daily.get("temperature_2m_max", [None])[i]
    return None


# -----------------------------------------------------------------------
# Signal dataclass and main builder
# -----------------------------------------------------------------------

@dataclass
class WeatherSignal:
    condition_id: str
    title: str
    outcome: str
    event_slug: str
    market_slug: str
    city: str
    station_name: str
    target_date: str
    is_high: bool
    cur_price: float
    model_prob: float
    edge: float            # model_prob - cur_price (positive = model thinks market underpriced this bucket)
    forecast_temp: float
    unit: str
    bucket_low: float
    bucket_high: float


def build_signals(markets: list, min_edge: float) -> list:
    signals = []
    cache = {}  # (lat, lon, date, unit) -> forecast_temp

    from calibration_backtest import parse_list_field  # reuse the safe JSON/list parser

    for m in markets:
        title = m.get("question", "")
        city = parse_city(title)
        if not city:
            continue
        target_date = parse_date(title)
        if not target_date:
            continue
        is_high = is_high_temp(title)

        resolution_text = m.get("description", "") or m.get("resolution", "") or ""
        station = lookup_station(city, resolution_text)
        if not station:
            continue

        days_ahead = (target_date.date() - datetime.now(timezone.utc).date()).days
        sigma = SIGMA_BY_DAYS.get(min(days_ahead, 5), 5.0)

        cache_key = (station["lat"], station["lon"], target_date.strftime("%Y-%m-%d"), station["unit"])
        if cache_key not in cache:
            t = fetch_forecast_temp(station["lat"], station["lon"], target_date, station["unit"])
            cache[cache_key] = t
            time.sleep(0.1)
        forecast_temp = cache[cache_key]
        if forecast_temp is None:
            continue

        outcomes = parse_list_field(m.get("outcomes", "[]"))
        outcome_prices = parse_list_field(m.get("outcomePrices", "[]"))

        for outcome, price_str in zip(outcomes, outcome_prices):
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue
            if not (0.01 < price < 0.99):
                continue

            bucket = parse_outcome_bucket(outcome, station["unit"])
            if bucket is None:
                continue
            low, high = bucket

            model_prob = bucket_probability(forecast_temp, sigma, low, high)
            edge = model_prob - price

            if abs(edge) < min_edge:
                continue

            signals.append(WeatherSignal(
                condition_id=m.get("conditionId", ""),
                title=title, outcome=outcome,
                event_slug=m.get("slug", ""),
                market_slug=m.get("slug", ""),
                city=city, station_name=station["name"],
                target_date=target_date.strftime("%Y-%m-%d"),
                is_high=is_high, cur_price=price,
                model_prob=round(model_prob, 4), edge=round(edge, 4),
                forecast_temp=forecast_temp, unit=station["unit"],
                bucket_low=low, bucket_high=high,
            ))

    signals.sort(key=lambda s: abs(s.edge), reverse=True)
    return signals


def print_report(signals: list, show: int):
    if not signals:
        print("\nNo temperature bucket mispricings found at the current threshold.")
        return
    deg = lambda u: "°F" if u == "fahrenheit" else "°C"
    print(f"\n{'='*90}\nWEATHER TEMPERATURE SIGNALS  (top {show} of {len(signals)})\n{'='*90}")
    for s in signals[:show]:
        kind = "HIGH" if s.is_high else "LOW"
        direction = "BUY" if s.edge > 0 else "FADE (buy No)"
        print(f"\n[{direction}] {s.city} {kind} temp {s.target_date}  ->  outcome \"{s.outcome}\"")
        print(f"  station: {s.station_name}  |  model forecast: {s.forecast_temp:.1f}{deg(s.unit)}"
              f"  |  bucket [{s.bucket_low:.0f}-{s.bucket_high:.0f}]")
        print(f"  market price: {s.cur_price:.3f}  |  model prob: {s.model_prob:.3f}  |  "
              f"edge: {s.edge:+.3f}")
        print(f"  https://polymarket.com/event/{s.event_slug}")


def send_discord(signals: list, webhook_url: str, top_n: int):
    deg_sym = lambda u: "°F" if u == "fahrenheit" else "°C"
    if not signals:
        requests.post(webhook_url,
                      json={"content": "Weather temp scan ran -- no mispriced buckets found today."},
                      timeout=15).raise_for_status()
        return
    fields = []
    for s in signals[:min(top_n, 10)]:
        kind = "HIGH" if s.is_high else "LOW"
        direction = "BUY" if s.edge > 0 else "FADE"
        fields.append({
            "name": f"[{direction}] {s.city} {kind} {s.target_date} → \"{s.outcome}\"",
            "value": (f"Station: {s.station_name} | forecast {s.forecast_temp:.1f}{deg_sym(s.unit)}\n"
                      f"market {s.cur_price:.2f} → model {s.model_prob:.2f} | edge {s.edge:+.3f}\n"
                      f"https://polymarket.com/event/{s.event_slug}"),
            "inline": False,
        })
    embed = {"title": f"Weather Temp Signals ({len(signals)} found)", "color": 1752220, "fields": fields}
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    resp.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Find mispriced Polymarket temperature bucket markets.")
    ap.add_argument("--max-days", type=int, default=3, help="only look at markets resolving within this many days")
    ap.add_argument("--max-markets", type=int, default=200)
    ap.add_argument("--min-edge", type=float, default=0.07, help="minimum |model - market| to flag (default 7pp)")
    ap.add_argument("--show", type=int, default=15)
    ap.add_argument("--json", help="optional output path")
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    args = ap.parse_args()

    print(f"Fetching weather temperature markets resolving within {args.max_days} day(s)...")
    markets = fetch_weather_markets(args.max_markets, args.max_days)
    print(f"Found {len(markets)} temperature markets. Pulling forecasts and computing edges...")

    signals = build_signals(markets, args.min_edge)
    print_report(signals, args.show)

    if args.json:
        payload = [
            {"city": s.city, "station": s.station_name, "date": s.target_date,
             "outcome": s.outcome, "cur_price": s.cur_price, "model_prob": s.model_prob,
             "edge": s.edge, "forecast_temp": s.forecast_temp, "unit": s.unit,
             "event_url": f"https://polymarket.com/event/{s.event_slug}"}
            for s in signals
        ]
        with open(args.json, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {len(signals)} signals to {args.json}")

    if args.discord_webhook:
        send_discord(signals, args.discord_webhook, args.show)
        print("Posted to Discord.")


if __name__ == "__main__":
    main()
