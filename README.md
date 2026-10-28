# eBay hardware deal scanner

A personal-use scanner for underpriced GPUs and PC hardware on eBay Italy,
Germany and Austria. It finds listings, identifies the exact product, excludes
broken items and accessories, estimates a realistic fair value with an explicit
evidence source, and sends a deduplicated Telegram alert when the total cost is
genuinely below that value.

Purchases and bids stay manual. There is no auto-buying.

## What it does differently

The scanner does not take the median of every search result. The pipeline is:

1. **Discover** exact models, auctions ending soon, curated variants, mixed
   lots and broad category searches on a budget-aware schedule.
2. **Ingest** each listing as a canonical record plus an append-only
   observation history. Price changes are retained, not overwritten.
3. **Identify** the exact product with a deterministic matcher. `RTX 3090`,
   `RTX 3090 Ti`, `RTX 3080 Ti`, `RX 7900 XT` and `RX 7900 XTX` are separate
   identities. Ambiguous titles are not forced into a product.
4. **Classify** the listing as complete, bundle, accessory, defect, empty box or
   ambiguous. Defective and packaging-only listings never receive a working
   valuation.
5. **Value** the exact product and condition from verified sold observations
   first, then a verified manual reference, then (only provisionally) active
   asking prices. Median and IQR outlier handling are used. The source, sample
   size, window and confidence are recorded with every estimate.
6. **Rank** with separate `deal_score`, confidence and risk. A critical risk
   flag cannot be bought off with a large discount.
7. **Alert** through Telegram with pending/delivered/failed states. Delivery is
   only marked successful after Telegram confirms it. Deduplication suppresses
   the same unchanged listing and only re-alerts on a material price drop or a
   changed risk assessment.

Official sold data (Marketplace Insights) is restricted by eBay and is not
required. Active listings are labeled as asking-price evidence and are never
presented as verified resale value.

## Requirements

- An eBay Developers Program application with **production** Client ID and
  Client Secret. Browse API access uses the client-credentials grant. See the
  [eBay Browse API](https://developer.ebay.com/api-docs/buy/browse/overview.html).
- A Telegram bot token and the chat ID that should receive alerts.
- Docker with Compose, or Python 3.12+ for local development.
- No GPU, paid hosting, database server or LLM.

eBay documents a default Browse allowance of 5,000 calls/day. The scanner keeps
its own hard ledger and derives the polling schedule from your configured
budget rather than spending first and discovering the limit later.

## Quick start

```bash
cp .env.example .env
mkdir -p data

# Linux only: the container runs as UID/GID 10001 by default.
sudo chown -R 10001:10001 data

# Fill in EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
$EDITOR .env

# 1. Prove API access before enabling the scanner.
docker compose run --rm ebay-scanner feasibility --telegram

# 2. Start in shadow mode (the default). Candidates are logged, no messages sent.
docker compose up -d --build
docker compose logs -f ebay-scanner
```

The feasibility command checks, with real production calls:

- OAuth client-credentials token issuance.
- Italian marketplace search and usable listing fields.
- German and Austrian searches and per-offer shipping data for Italy.
- Item detail access (description, aspects, shipping signals).
- Taxonomy access for category-scoped broad searches.
- Whether your planned schedule fits your configured daily budget.

It writes `data/feasibility-report.json` and exits non-zero if a required check
fails. `--telegram` also sends a test message.

### Shadow mode first

`SHADOW_MODE=true` is deliberate and is the recommended starting point. The
scanner discovers, values and scores listings, logs candidates and records
them as `shadow` alerts, but does not contact Telegram.

Review the shadow candidates, correct the catalogue/references and only then:

```dotenv
SHADOW_MODE=false
```

Live mode refuses to start without a Telegram token and chat ID.

## Reference prices are placeholders

`config/references.yaml` ships with illustrative numbers marked
`verified: false`. Those entries are provisional evidence only: they are shown
in shadow logs but are **not** enough for a normal alert. This is intentional.

Populate real evidence in one of two ways:

```bash
# Import a verified sold observation (repeat several times, ideally 5-8+).
docker compose exec ebay-scanner python -m src.main add-comparable \
  --product rtx_3090 --price 480 --condition used_working \
  --source "eBay sold items, October 2026" --verified

# Or edit config/references.yaml
#   price_eur, verified: true, sample_size, observed_at, source
docker compose restart ebay-scanner
```

The estimator prefers verified sold observations from the last 90 days. With
8+ observations it reports high confidence; 5-7 medium; fewer than 5 sales falls
back to a verified reference or low confidence. Active listings can only ever
produce a provisional estimate, and provisional estimates do not alert unless
you explicitly change `alerts.allow_provisional`.

Reference prices must be kept separate by condition. Never use defective or
parts-only prices to value a working item.

## Configuration

All operator-facing settings live in `config/`:

| File | Purpose |
| --- | --- |
| `products.yaml` | Marketplaces, categories, search products/aliases/queries, bundles, broad searches, budgets, scan intervals, alert/ranking thresholds, retention, taxonomy settings |
| `references.yaml` | Verified or provisional fair-value evidence per product and condition |
| `exclusions.yaml` | Multilingual defect, packaging, accessory, bundle and ambiguity vocabulary |

The catalogue contains GPUs, CPUs, RAM, storage and a motherboard example. Add
an exact product with its aliases, a canonical model phrase and a query; the
matcher normalizes spacing, punctuation and German umlauts, so `RTX3090Ti` and
`rtx 3090 ti` are comparable while remaining a different identity from
`RTX 3090`.

### Searching within the API budget

```bash
docker compose run --rm ebay-scanner python -m src.main plan
```

This prints the derived intervals, planned calls/day, available calls after
the reserve and detail cap, and any stretch factor. High-priority products scan
more often than normal ones. The scheduler:

- records every HTTP attempt by endpoint, marketplace and UTC day;
- stops scheduled searches before the reserve is exhausted;
- runs high-priority jobs first and limits each cycle to 8 jobs so heartbeats
  and alert delivery keep running;
- prevents overlapping executions with a persisted job lock;
- backs off exponentially with jitter and honors `Retry-After` on HTTP 429;
- refreshes OAuth tokens before expiry and after a 401;
- stores the last success and last error for every job.

Pagination is deliberately shallow. A 50-item page is not treated as the whole
result set.

### Category filters

Broad and bundle searches can use eBay Taxonomy category suggestions. Exact
model searches remain unrestricted so a wrong category assignment cannot hide
a deal. Taxonomy results are cached in SQLite for 30 days. If Taxonomy is
unavailable, affected searches continue without a category filter.

## Commands

`docker compose exec ebay-scanner python -m src.main <command>`:

| Command | Description |
| --- | --- |
| `run` | Continuous scheduler and alert delivery (container default) |
| `feasibility [--telegram]` | API proof of concept and budget check |
| `plan [--json]` | Derived search schedule and API budget |
| `once [--strategy S] [--limit N]` | Run N profiles once for diagnostics |
| `report [--days N] [--json]` | Alert quality, decisions, API usage, stale coverage |
| `add-comparable ...` | Import a sold/observed price for valuation |
| `record-decision ...` | Record manual feedback (`good_deal`, `wrong_product`, etc.) |
| `notify-test` | Send a Telegram test message |
| `backup` | Consistent SQLite backup into `data/backups/` |
| `prune` | Apply retention limits now |
| `health` | Container health check |

Feedback closes the quality loop:

```bash
docker compose exec ebay-scanner python -m src.main record-decision \
  --item-id 123456789 --decision good_deal --reason "bought for personal build"
docker compose exec ebay-scanner python -m src.main report --days 14
```

`report` shows precision, false-positive reasons, average first-seen-to-alert
delay, API calls per delivered alert, calls per good decision, per-endpoint
usage and jobs with stale coverage. Do not judge the scanner by alert volume.

### Backups and retention

The database is a bind-mounted SQLite file at `data/scanner.db`. The service:

- runs a SQLite online backup once per UTC day into `data/backups/`;
- keeps `retention.backup_days` of backups (14 by default);
- prunes observations, delivered/shadow/failed alerts and old API ledger rows;
- never claims a listing sold just because it disappeared from a search. It
  moves to `not_seen` after 72 hours and `unknown` after 14 days.

Raw API responses are not archived indefinitely. Only the fields needed for
discovery, risk, valuation, alerts and metrics are stored.

## Docker details

- Multi-stage image, non-root user `10001:10001`.
- Read-only root filesystem, read-only config mount, dropped capabilities,
  `no-new-privileges`, bounded memory/CPU/PIDs and capped JSON logs.
- SQLite/data is the only writable bind mount. Telegram is outbound only, so no
  ports or reverse proxy are needed.
- Health check: database readable, schema present, scheduler heartbeat fresh
  (`HEALTH_MAX_AGE_SECONDS`, default 900).
- Graceful shutdown on SIGTERM/SIGINT with a 30-second stop grace period.

If the container cannot write `./data` on Linux, fix ownership:

```bash
sudo chown -R 10001:10001 data
# or set SCANNER_UID/SCANNER_GID to the host owner in .env or the shell
```

Run the test suite inside the image build:

```bash
docker build --target test -t ebay-scanner-test .
docker run --rm ebay-scanner-test
```

Or locally:

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Local web dashboard

A minimal, dependency-free dashboard reads the same SQLite database the
scanner writes:

```bash
# next to the scanner container (GUI_BIND/GUI_PORT control the bridge)
docker compose up -d gui
# then open http://127.0.0.1:8080

# or without Docker, in the project (reads DATABASE_PATH)
python -m src.main gui --host 127.0.0.1 --port 8080
```

It shows live stats (listings, alerts, daily API budget, scheduler heartbeat),
a filterable/sortable listings table (product, marketplace, condition, risk,
min discount, min savings, max total cost, auctions ending soon), pending and
delivered alerts, and per-listing manual review decisions (the same decisions
as `record-decision`) for feedback-driven tuning.

Security notes:

- The dashboard is read-only except for manual decisions; it talks only to
  the local SQLite file.
- The compose file binds it to host loopback (`GUI_BIND=127.0.0.1`) by
  default. Only set `GUI_BIND=0.0.0.0` on a trusted LAN; the UI has no
  authentication. eBay/Telegram credentials are never exposed through it.
- It runs as a separate container sharing `./data`, so the scanner's memory
  and CPU limits are unaffected.

## Security

- `.env` is git-ignored and excluded from the image context. Secrets are only
  read from the environment; they are never written to SQLite or logs.
- Telegram and eBay credentials are not exposed to the browser because there is
  no browser surface.
- eBay item IDs, titles and seller identifiers are the only marketplace data
  stored. Check the applicable eBay API terms for your retention obligations.

## Limitations

- eBay's sold-data API is restricted; the default evidence path is verified
  manual references plus your imported observations.
- Used-hardware prices move. Re-verify references after GPU/CPU launches.
- Delivery eligibility is checked from eBay's `deliveryCountry` search filter
  and per-offer shipping data. Sellers can still change terms; verify before
  buying.
- Import VAT is estimated (22% for Italy) when a non-EU seller provides no
  import charge. It is never added to normal intra-EU purchases.
- Image verification, LLM title understanding and automatic bidding are
  intentionally out of scope for the first version. The local dashboard is
  read-only over the same database and does not change this.

## Layout

```text
src/
  main.py                 CLI, feasibility, health, runtime wiring
  config.py               env + YAML validation
  pipeline.py             discovery -> ingestion -> matching -> valuation -> alert
  ebay/                   OAuth, Browse/Taxonomy client, quota ledger, parsing
  discovery/              search profiles, budget planner, scheduler
  products/               title normalization, exact matcher, risk classifier
  pricing/                FX, total cost, comparables, estimator
  ranking/scorer.py       deal score, confidence, risk, auction max bid
  storage/                SQLite schema and repository
  alerts/                 Telegram formatting, dedupe, delivery states
  gui/                    local web dashboard (stdlib http.server + vanilla JS)
  monitoring/             JSON logging and quality metrics
config/
  products.yaml
  references.yaml
  exclusions.yaml
tests/
Dockerfile
compose.yaml
```
