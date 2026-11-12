# Kalshi Lab — paper prediction experiment

A public, forward-only experiment: collect real Kalshi quotes and public evidence, freeze predictions before outcomes, simulate entries, and measure net paper results after official settlement. No real-money orders, deposits or Kalshi trading credentials.

Dashboard: https://zero-lvl.github.io/kalshi-lab-dashboard/

## Repository layout

- Root HTML/JS/CSS and `dashboard-data.json`: the read-only GitHub Pages dashboard and sanitized observed paper history.
- `engine/`: reviewed Python source, frontend source and isolated fixture tests. Operational databases, machine configuration, approval records and raw news/input logs are not published.
- The backend runs on the operator's machine, not inside GitHub Pages. Committed dashboard data is a timestamped snapshot, not a live exchange feed.

## Run the experiment locally

Python 3.11+; standard library only. From a checkout:

```sh
cd engine
python3 -m unittest discover -s tests -q
python3 engine.py scan
python3 engine.py report --full
python3 engine.py scan --settlement-only
python3 -m http.server 8767 --bind 127.0.0.1 --directory site
```

The first scan uses actual public HTTPS sources and creates your own local `data/paper.db`. Tests use isolated synthetic fixtures, never the operator's production ledger. The paper bankroll starts at $1,000. Do not reset the database to erase losses.

## Research methods

- Weather: supported daily-high stations; Open-Meteo and NOAA/NWS inputs; strict station/date/rules checks and local cutoff. Gaussian uncertainty is an unvalidated assumption, not a fitted probability guarantee.
- BTC/ETH: terminal-price lognormal approximation using actual completed Coinbase daily bars and fresh spot quotes. Coinbase is a proxy, not official CF Benchmarks settlement; jumps, basis and rounding can matter.
- Politics/Economics/Technology: tiny explicitly labeled market-consensus baseline controls on supported contracts. The technology grouping includes the technology-heavy Nasdaq-100, not a claim about Kalshi's official category. These controls have no independent predictive edge and can have negative expected net value after costs.
- RSS headlines/descriptions: timestamped topical evidence with original links. Relevance is not measured causal correlation or a directional trading signal. Unsupported contracts abstain.

## Accounting and limits

- At most $20 estimated entry cost per position and $100 total open cost; $40 category cap for new entries. Older positions are not rewritten or forcibly closed when limits change.
- Consensus controls are limited to $2 per position and $10 open cost per category. At most one position per event ever, whole contracts only, no leverage.
- Entry/exit slippage and quadratic fees are estimates; visible quote depth is a cap, not proof of a fill. No queue/latency, tax or cash-interest model.
- Only official final binary Kalshi outcomes settle a position. Missing/fractional/ambiguous outcomes remain unresolved. Final outcomes are frozen on first observation; later exchange corrections are not automatically replayed.
- Net win is payout minus entry cost including estimated fee; net loss is the full entry cost. Open positions are not wins or losses.
- Realized P&L is the default curve. Open/total liquidation P&L is unknown when a valid bid/depth is missing. Cash plus entry cost is not marked-to-market profit.
- Day/week/month totals use Pacific calendar boundaries. Chart windows are rolling 24h, 7d, 30d and 90d, with no invented pre-inception history.
- Scenario simulations use immutable entry probabilities and an independent-event assumption. They are model-implied possibilities, not earned returns.

## Learning and audit

Original forecasts, entry inputs and decisions are preserved. Scoring compares pre-outcome estimates with market-odds baselines and official outcomes; correlated buckets are not independent events. Conservative adaptation uses separate 30-event training and 20-event holdout cohorts. Sustained losses in both suspend future strategy/category entries; it does not automatically increase stakes, resume a failed strategy or retune all models.

The public dashboard includes all paper positions, results, costs, evidence links, portfolio curves, calendar totals, filters and strategy diagnostics. Its **Research** section includes the complete recorded history of decisions, forecasts, news observations, topical news matches and scans in stable 100-record pages. Newest records load first; Older/Newer reaches the full history. Search and action filters apply to the displayed page, with page-specific CSV export. Original forecasts remain frozen; official outcomes are annotations, and a forecast is not a trade. Only allowlisted research fields/headlines/source links are published—not credentials, raw payloads, full articles, machine paths or operational traces. Records from before detailed decision logging may lack a decision entry; no historical rows are invented. No demonstrated profitability is claimed.

## Operator automation

New-entry research is scheduled twice daily at 05:05/17:05 Pacific. Known outcomes/quotes refresh hourly; the full daily Discord report runs at 09:15 Pacific. The machine, network and scheduler must be running. Missed observations cannot be reconstructed honestly.

The optional `publisher.py` is operator-gated: exact approved destination, clean isolated workspace, manifest/privacy checks, one effective push destination, no unpublished history, no force push and remote commit readback. Recurring publication changes only the five root dashboard artifacts; `engine/` remains a frozen reviewed source snapshot. A pushed snapshot is not proof that Pages has deployed it.
