# Polymarket Calibration Bias Bot

A third, independent bot in this repo, and a fundamentally different
mechanism from the other two: instead of copying other traders
(consensus bot) or watching company insiders (insider tracker), this one
checks whether **Polymarket itself is systematically mispriced** at
certain price levels, using only Polymarket's own historical data.

## The idea

Betting markets have a long-documented anomaly called the
**favorite-longshot bias**: outcomes priced as longshots (say, 3%) tend
to happen *less* often than 3% of the time, because people overpay for
lottery-ticket-style bets, and outcomes priced as heavy favorites (say,
93%) tend to happen *more* often than 93% of the time, because that
same overpayment for longshots pulls money away from favorites. This has
been studied across horse racing, sports betting, and other prediction
markets for decades.

This bot tests whether Polymarket shows the same pattern, using its own
resolved markets as the dataset, and if so, bets accordingly on
currently-open markets sitting in a mispriced price bucket. It's a
two-stage system:

## Stage 1 — `calibration_backtest.py` (weekly)

Pulls a large sample of already-CLOSED, binary (Yes/No) markets, looks
up each one's price a few days before it actually resolved (not the
final, near-certain price), buckets them by that price (e.g. "92-95%
priced"), and compares the bucket's *average priced probability* against
how often that outcome *actually happened*. A real, sustained gap
between the two — backed by enough sample size to trust statistically —
is the edge.

```bash
pip install requests
python calibration_backtest.py
```

```bash
# Tune the backtest
python calibration_backtest.py --max-markets 3000 --days-before 7 --min-volume 10000
```

| Flag | Default | What it does |
|---|---|---|
| `--max-markets` | 1500 | how many resolved binary markets to sample |
| `--min-volume` | 5000 | ignore markets with less total trading volume |
| `--days-before` | 3 | how many days before resolution to sample the price |
| `--min-bucket-n` | 30 | minimum sample size before a bucket's edge is trusted |
| `--out` | calibration_table.json | output path |

Output looks like this (a real run will have its own numbers):

```
        bucket |      n | avg priced | actual rate |     edge |  edge/SE
----------------------------------------------------------------------
          2-5% |    340 |       3.4% |        1.8% |   -1.60% |    -2.10  **
        35-50% |    410 |      42.3% |       41.7% |   -0.60% |    -0.41
        90-95% |    355 |      92.5% |       96.2% |   +3.70% |    +3.85  **
```

`**` means reasonably confident that bucket is mispriced (`|edge/SE| >=
2` and enough sample size). This table is what Stage 2 uses to actually
trade — only buckets with enough evidence get used.

## Stage 2 — `calibration_signals.py` + `calibration_paper_trader.py` (daily)

`calibration_signals.py` scans currently-open binary markets and flags
any one priced inside a bucket Stage 1 found to be mispriced — buying
whichever side (Yes or No) the historical edge favors.

```bash
python calibration_signals.py
```

`calibration_paper_trader.py` paper-trades those signals, settling
against the market's real resolution exactly the same way the original
consensus bot does (it actually reuses that bot's settlement code
directly rather than duplicating it).

```bash
python calibration_paper_trader.py
```

| Flag | Default | What it does |
|---|---|---|
| `--min-edge` | 0.03 | minimum mispricing (3 percentage points) to flag |
| `--min-z` | 1.5 | minimum statistical confidence (edge ÷ standard error) |
| `--min-bucket-n` | 30 | minimum historical sample size to trust a bucket |
| `--stake` | 20 | $ size per new paper position |
| `--max-open` | 20 | cap on simultaneous open positions |

## Running it automated

Two separate workflows, because the two stages run on different
schedules:

- `.github/workflows/calibration-weekly.yml` — rebuilds the calibration
  table every Sunday (this is a slow-moving structural property of the
  market, not something that needs checking daily).
- `.github/workflows/calibration-daily.yml` — scans for and paper-trades
  signals every day, charts the results, posts to Discord.

**Setup:**

1. Add a repo secret `DISCORD_WEBHOOK_URL_CALIBRATION` (its own Discord
   channel, same process as the other bots: gear icon → Integrations →
   Webhooks → New Webhook → Copy URL).
2. **Run the weekly backtest manually first** (Actions tab → "Weekly
   Calibration Bias Backtest" → Run workflow) — the daily trader needs
   `calibration_table.json` to exist before it has anything to act on,
   and will fail with a clear error message if you skip this step.
3. Then run the daily one manually to confirm it works, same as the
   other bots.

## Honest limitations

- **Smaller, slower-moving edge than the other two bots.** Even a real
  favorite-longshot bias is typically a few percentage points, not a
  huge gap — don't expect dramatic swings either direction.
- **Backtest sample quality matters a lot.** 1500 markets sounds like a
  lot, but split across 12 price buckets it's roughly 100-150 per
  bucket on average, and unevenly distributed (extreme buckets near 0%
  or 100% tend to have fewer qualifying samples than the middle).
  Increase `--max-markets` over time as you get a feel for which buckets
  are data-starved.
- **The 3-day-before-resolution snapshot is a judgment call, not a
  proven-optimal choice.** Too close to resolution and you're just
  reading an outcome that's already basically known; too far and you're
  measuring something closer to "long-run market drift" than genuine
  mispricing. Worth experimenting with `--days-before` once you have a
  baseline.
- **Binary markets only**, by design — see the comment at the top of
  `calibration_backtest.py` for why multi-outcome markets are excluded.
- **This edge, if real, is structural and slow.** It's not a reason to
  expect daily action — some days will have zero qualifying signals,
  and that's the strategy working correctly, not underperforming.
- Same blanket caveat as the other two bots: paper trading first,
  real money only if and when the numbers actually convince you.
