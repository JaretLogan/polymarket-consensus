#!/usr/bin/env python3
"""
SEC Insider Buying Signal Finder
------------------------------------
Pulls recent Form 4 filings (the disclosures executives/directors/10%
owners must file within 2 business days of trading their own company's
stock) straight from SEC EDGAR, parses the structured XML each filing
contains, and flags stocks where insiders made genuine OPEN-MARKET
PURCHASES (transaction code 'P', shares acquired) in the lookback window.

Why purchases and not sales: insider selling happens for all sorts of
mundane reasons (taxes, diversification, scheduled 10b5-1 plans, buying a
house) and is a weak/noisy signal. An open-market purchase is voluntary
and the insider is spending their own money -- a much more deliberate
vote of confidence. This is also a genuinely studied academic effect
(see Lakonishok & Lee 2001 and follow-on work), not just a TikTok claim.

Uses only SEC EDGAR's free public endpoints. No API key required, but
SEC's fair-access policy REQUIRES a descriptive User-Agent on every
request (e.g. "Your Name your-email@example.com") -- generic or missing
User-Agents get rate-limited or blocked. Set yours via --user-agent or
the SEC_USER_AGENT environment variable before running.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

GETCURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ATOM_NS = "{http://www.w3.org/2005/Atom}"

DEFAULT_USER_AGENT = "SET_YOUR_CONTACT_INFO_SEE_README your-email@example.com"


def get_headers(user_agent: str) -> dict:
    if not user_agent or "SET_YOUR_CONTACT_INFO" in user_agent:
        print(
            "\n[error] SEC requires a real, descriptive User-Agent on every request\n"
            "(e.g. 'Jaret Logan your-email@example.com'). Set one with --user-agent\n"
            "or the SEC_USER_AGENT environment variable. Generic/missing User-Agents\n"
            "get rate-limited or blocked by SEC -- this isn't optional.\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return {"User-Agent": user_agent}


@dataclass
class Transaction:
    accession: str
    cik: str
    ticker: str
    issuer_name: str
    owner_name: str
    title: str
    is_officer: bool
    is_director: bool
    is_ten_pct_owner: bool
    transaction_date: str
    shares: float
    price: float
    shares_owned_after: float

    @property
    def value(self) -> float:
        return self.shares * self.price


@dataclass
class BuySignal:
    ticker: str
    issuer_name: str
    insider_count: int = 0
    total_value: float = 0.0
    transactions: list = field(default_factory=list)


def _get_with_retry(url, headers, params=None, timeout=20, retries=2):
    """SEC occasionally 429s under load; one short backoff retry is enough."""
    for attempt in range(retries + 1):
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code == 429 and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        return resp
    return resp


def fetch_recent_filings(headers: dict, lookback_hours: float, max_pages: int) -> list:
    """
    Page through EDGAR's 'latest filings' atom feed filtered to Form 4,
    dedupe by accession number (each filing appears once per associated
    entity -- issuer AND reporting owner -- so raw entries double-count),
    stop once a page's oldest entry falls outside the lookback window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    seen = {}
    start = 0
    page_size = 100
    link_re = re.compile(r"/data/(\d+)/(\d+)/[^/]*-index\.htm")

    for _ in range(max_pages):
        params = {
            "action": "getcurrent",
            "type": "4",
            "company": "",
            "dateb": "",
            "owner": "include",
            "count": page_size,
            "start": start,
            "output": "atom",
        }
        resp = _get_with_retry(GETCURRENT_URL, headers, params=params)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        entries = root.findall(f"{ATOM_NS}entry")
        if not entries:
            break

        oldest_on_page = None
        for entry in entries:
            link_el = entry.find(f"{ATOM_NS}link")
            updated_el = entry.find(f"{ATOM_NS}updated")
            if link_el is None or updated_el is None or updated_el.text is None:
                continue
            href = link_el.get("href", "")
            m = link_re.search(href)
            if not m:
                continue
            cik, accession_nodash = m.group(1), m.group(2)

            try:
                updated = datetime.fromisoformat(updated_el.text.strip())
            except ValueError:
                continue
            if oldest_on_page is None or updated < oldest_on_page:
                oldest_on_page = updated

            if accession_nodash not in seen:
                seen[accession_nodash] = {"cik": cik, "accession": accession_nodash, "updated": updated}

        if oldest_on_page is not None and oldest_on_page < cutoff:
            break
        start += page_size
        time.sleep(0.15)

    return [f for f in seen.values() if f["updated"] >= cutoff]


def fetch_form4_xml(headers: dict, cik: str, accession_nodash: str):
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/primary_doc.xml"
    resp = _get_with_retry(url, headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def _text(elem, path):
    if elem is None:
        return None
    node = elem.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _float(elem, path, default=0.0):
    val = _text(elem, path)
    try:
        return float(val) if val is not None else default
    except ValueError:
        return default


def parse_form4(xml_bytes: bytes, accession: str, cik: str) -> list:
    """Returns every open-market BUY (code 'P', acquired 'A') in this
    filing's non-derivative (actual common stock, not options) table."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    ticker = _text(root, "./issuer/issuerTradingSymbol") or "?"
    issuer_name = _text(root, "./issuer/issuerName") or "Unknown"

    owner = root.find("./reportingOwner")
    if owner is None:
        return []
    owner_name = _text(owner, "./reportingOwnerId/rptOwnerName") or "Unknown"
    rel = owner.find("./reportingOwnerRelationship")
    is_officer = _text(rel, "./isOfficer") == "1"
    is_director = _text(rel, "./isDirector") == "1"
    is_ten_pct = _text(rel, "./isTenPercentOwner") == "1"
    title = _text(rel, "./officerTitle") or ("Director" if is_director else "10% Owner" if is_ten_pct else "")

    out = []
    for tx in root.findall("./nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "./transactionCoding/transactionCode")
        acquired = _text(tx, "./transactionAmounts/transactionAcquiredDisposedCode/value")
        if code != "P" or acquired != "A":
            continue
        out.append(Transaction(
            accession=accession, cik=cik, ticker=ticker.upper(), issuer_name=issuer_name,
            owner_name=owner_name, title=title, is_officer=is_officer, is_director=is_director,
            is_ten_pct_owner=is_ten_pct,
            transaction_date=_text(tx, "./transactionDate/value") or "",
            shares=_float(tx, "./transactionAmounts/transactionShares/value"),
            price=_float(tx, "./transactionAmounts/transactionPricePerShare/value"),
            shares_owned_after=_float(tx, "./postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
        ))
    return out


def build_signals(headers: dict, filings: list, min_value: float, min_insiders: int) -> list:
    by_ticker = {}
    for i, f in enumerate(filings, start=1):
        if i % 25 == 0 or i == len(filings):
            print(f"  [{i}/{len(filings)}] filings parsed...")
        try:
            xml_bytes = fetch_form4_xml(headers, f["cik"], f["accession"])
        except requests.RequestException as e:
            print(f"  [warn] failed to fetch {f['accession']}: {e}", file=sys.stderr)
            continue
        if xml_bytes is None:
            continue

        for tx in parse_form4(xml_bytes, f["accession"], f["cik"]):
            if tx.value < min_value:
                continue
            if tx.ticker not in by_ticker:
                by_ticker[tx.ticker] = BuySignal(ticker=tx.ticker, issuer_name=tx.issuer_name)
            by_ticker[tx.ticker].transactions.append(tx)

        time.sleep(0.12)  # stay well under SEC's fair-access rate limits

    results = []
    for sig in by_ticker.values():
        distinct_insiders = {t.owner_name for t in sig.transactions}
        sig.insider_count = len(distinct_insiders)
        sig.total_value = round(sum(t.value for t in sig.transactions), 2)
        if sig.insider_count >= min_insiders:
            results.append(sig)

    results.sort(key=lambda s: (s.insider_count, s.total_value), reverse=True)
    return results


def print_report(results: list, show: int):
    if not results:
        print("\nNo qualifying insider buy signals found at the current thresholds.")
        return
    print(f"\n{'='*90}\nINSIDER BUY SIGNALS  (top {show} of {len(results)} qualifying)\n{'='*90}")
    for s in results[:show]:
        print(f"\n[{s.insider_count} insider(s)] {s.ticker} -- {s.issuer_name}")
        print(f"  total open-market buy value: ${s.total_value:,.2f}")
        for t in sorted(s.transactions, key=lambda t: -t.value)[:6]:
            print(f"    {t.owner_name} ({t.title}): {t.shares:,.0f} sh @ ${t.price:.2f} "
                  f"= ${t.value:,.0f}  on {t.transaction_date}")


def write_csv(results: list, path: str):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "issuer_name", "insider_count", "total_value", "insiders"])
        for s in results:
            insiders = "; ".join(f"{t.owner_name} ({t.title}):${t.value:,.0f}" for t in s.transactions)
            writer.writerow([s.ticker, s.issuer_name, s.insider_count, s.total_value, insiders])


def write_json(results: list, path: str):
    payload = [
        {
            "ticker": s.ticker,
            "issuer_name": s.issuer_name,
            "insider_count": s.insider_count,
            "total_value": s.total_value,
            "transactions": [
                {"owner_name": t.owner_name, "title": t.title, "shares": t.shares,
                 "price": t.price, "value": t.value, "date": t.transaction_date}
                for t in s.transactions
            ],
        }
        for s in results
    ]
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def send_discord(results: list, webhook_url: str, top_n: int):
    if not results:
        requests.post(webhook_url, json={"content": "Insider buy scan ran -- no qualifying signals today."},
                       timeout=15).raise_for_status()
        return

    fields = []
    for s in results[:min(top_n, 10)]:
        names = ", ".join(f"{t.owner_name} ({t.title})" for t in s.transactions[:4])
        if len(s.transactions) > 4:
            names += f" +{len(s.transactions) - 4} more"
        fields.append({
            "name": f"[{s.insider_count} insider(s)] {s.ticker} -- {s.issuer_name}",
            "value": f"${s.total_value:,.0f} total open-market buys\n{names}",
            "inline": False,
        })
    embed = {"title": f"SEC Insider Buy Signals ({len(results)} found)", "color": 3066993, "fields": fields}
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    resp.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Find clusters of SEC Form 4 open-market insider buys.")
    ap.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT))
    ap.add_argument("--lookback-hours", type=float, default=24.0)
    ap.add_argument("--max-pages", type=int, default=15, help="safety cap, 100 filings/page")
    ap.add_argument("--min-insiders", type=int, default=1, help="2+ means 'cluster' mode")
    ap.add_argument("--min-value", type=float, default=10000.0, help="ignore buys below this $ value")
    ap.add_argument("--show", type=int, default=15)
    ap.add_argument("--csv")
    ap.add_argument("--json")
    ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    args = ap.parse_args()

    headers = get_headers(args.user_agent)

    print(f"Pulling Form 4 filings from the last {args.lookback_hours}h...")
    filings = fetch_recent_filings(headers, args.lookback_hours, args.max_pages)
    print(f"Found {len(filings)} unique Form 4 filing(s) in window. Parsing each for open-market buys...")

    results = build_signals(headers, filings, args.min_value, args.min_insiders)
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
