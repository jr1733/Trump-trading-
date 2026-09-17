# Event → Market Intelligence

A mobile-first PWA that monitors public announcements and government information associated with
Donald Trump, and shows **how comparable past events related to market movements**.

> **This is a research and information tool.** It has no brokerage integration, places no orders,
> manages no portfolio, and takes no autonomous action. What it computes are statistical
> associations between past announcements and past price moves, from small and non-independent
> samples. **Association is not causation, and nothing here is a forecast or a recommendation.**

**Current status: Phase 3 complete.** See [Phase status](#phase-status) for exactly what works,
what is mocked, and what needs a key.

---

## Quick start (no API keys, no network)

```bash
cp .env.example .env
docker compose up --build
```

Open <http://localhost:8080> and sign in with the `APP_AUTH_TOKEN` from your `.env`
(`change-me-before-deploying` out of the box).

That gives you: the mock source, deterministic synthetic market data, canned offline analyses, a
286-event sample archive, a seeded watchlist and two alert rules. Everything in the UI is live —
none of it needs a key.

### Without Docker

**Postgres must have the `pgvector` extension** — embeddings are stored as
`vector` columns. On Debian/Ubuntu: `apt install postgresql-16-pgvector`. The bundled
compose file already uses the `pgvector/pgvector` image.

```bash
make install            # creates .venv and installs backend deps
createdb trumpmarket    # or point DATABASE_URL at any Postgres 14+ with pgvector
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/trumpmarket"
export APP_AUTH_TOKEN=devtoken LLM_FAKE_MODE=true ENABLED_SOURCES=mock

make migrate            # create the schema
make demo               # seed + archive + poll + pipeline
make api                # API on :8000

# in a second terminal
cd frontend && npm install && npm run dev   # UI on :5173, proxies /api to :8000
```

`make help` lists every target.

---

## What it does

```
SOURCE → NORMALISE → DEDUPLICATE → RELEVANCE (rules, then cheap model)
→ ENTITY EXTRACTION → TICKER MATCHING → ANALYSIS → STORE (stable id)
→ HISTORICAL RESPONSE → SIGNAL → EVALUATE NOTIFICATION RULES → DELIVER
```

Every stage is **idempotent**. Raw events are unique on `(source, external_id)`, events on their
content hash, analyses on their content hash, signals on `(event, ticker)`, and notifications on
an idempotency key. Re-running the worker over the same data produces no duplicate event, signal
or notification — there is an end-to-end test that asserts exactly this.

### Pages

**Live · Watchlist · Tickers · Search · Notifications · Backtest · Settings · Data quality**

| Page | What it shows |
|---|---|
| **Live** | Market context (SPY/QQQ/VIX/10y), top signals, source status, notification summary, and the event feed. Sort by newest, signal strength, ticker, source or event type. |
| **Ticker** (`/ticker/:symbol`) | Current signal with its "Why?" panel, event-time price chart, historical statistics per horizon, an on-demand **event study**, full event history with subsequent returns, and a one-tap watch toggle. |
| **Search** | Postgres full-text search over every event, each hit annotated with what the price did afterwards, plus "create alert from this search". |
| **Notifications** | Unread badge, read/unread, archive, delete, mark-all-read, and the per-channel delivery status of each notification. |
| **Event detail** | The full text, the model's fact/position/claim/speculation split, the signal, provenance timestamps, and the **similar past events** list with what each one was followed by. |
| **Settings** | Notification permission and push status, quiet hours, thresholds, digest settings, a test-notification button, source health, the live signal weights and embedding measure, and manual poll/pipeline/seed/embed/push-retry triggers. |
| **Backtest** | Run a point-in-time backtest over any window: pick tickers, event type, signal threshold, holding period, and rule-based or model sentiment. Chronological train/validation/test splits, mean/median/win-rate/SD/max-drawdown/Sharpe, overlap fraction, and a contamination banner when the model scored it. |
| **Data quality & cost** (`/data-quality`) | Pipeline backlog, missing embeddings, duplicate hashes, future timestamps, ticker-match confidence, market-data staleness, stale sources, malformed model output, processing errors, failed deliveries, recent worker jobs, and seven days of model spend extrapolated to a monthly figure. Reached from Settings. |

---

## Signal

A transparent score in `[-1, +1]`:

```
score = w1·sentiment
      + w2·historical_median_abnormal_return_normalised
      + w3·historical_consistency
      + w4·novelty
score = score × model_confidence × sample_size_factor
```

| Component | Definition |
|---|---|
| **sentiment** | The model's `sentiment` field, already in `[-1, 1]`. With no model available, the rule-based lexicon score stands in and the panel says so. |
| **historical** | Median abnormal return across comparable past events at the primary horizon, divided by `SIGNAL_RETURN_SCALE` (default 0.05 — a 5% median abnormal move is full scale), clamped to `[-1, 1]`. |
| **consistency** | How one-sided the sample is, signed by the median's direction: `2·max(pos%, neg%)/100 − 1`. A 50/50 split contributes 0; a 90/10 split contributes ±0.8. |
| **novelty** | `1 − max_similarity_to_the_last_30_days`, **signed in the direction the other components already point**. Novelty has no direction of its own: a novel event is not bullish, merely less anticipated, so it can only amplify an existing lean, never create one. |
| **model confidence** | Multiplies the whole score. A 0.3-confidence reading cannot produce a strong label. |
| **sample-size factor** | `0.0` below N=10, `0.6` for N=10–19, `1.0` at N≥20. Below N=10 the historical and consistency components are **dropped entirely and their weight redistributed**, rather than silently shrinking the score. |

Weights are renormalised over whichever components are active, so they always sum to 1. Market
context is deliberately omitted in Phase 1.

**Labels:** STRONGLY BULLISH ≥ 0.6 · BULLISH ≥ 0.2 · NEUTRAL · BEARISH ≤ −0.2 ·
STRONGLY BEARISH ≤ −0.6. All thresholds and weights are configurable and are displayed in
Settings.

### The "Why?" panel

Every signal carries one, and it is not optional. It lists each component's value, the weight
applied, the contribution to the score, the sample size and flag, the full historical statistics
per horizon, and **Important uncertainties** — small samples, non-independent events, regime
change, low ticker-match confidence, a near-duplicate event in the last 30 days, and the standing
reminder that association is not causation.

### Sample-size honesty

| N | Flag | Effect |
|---|---|---|
| < 10 | **unreliable** | Shown in the UI in red, given **zero weight** in the score. |
| 10–19 | **limited** | Shown in amber, score scaled by 0.6. |
| ≥ 20 | ok | Full weight. |

---

## Similarity and embeddings

Every event is embedded into a 384-dimensional vector stored in **pgvector**, and
similarity search runs in the database with the cosine distance operator against an HNSW
index. Similarity drives two things: the **similar past events** list on each event, and the
**novelty** component of the signal.

Two providers ship, and *which one produced a score is displayed next to the score*:

| Provider | Semantic? | Cost | Notes |
|---|---|---|---|
| `hashing` (**default**) | ❌ lexical | none | Hashed word/bigram/character n-grams, L2-normalised, fully deterministic. Genuinely related statements score around 0.30–0.45. No model download, no torch. |
| `sentence-transformers` | ✅ semantic | ~2 GB installed | A real local model (`all-MiniLM-L6-v2` by default). Anthropic has no embeddings API, so this runs on your own hardware. |

```bash
pip install -r backend/requirements-embeddings.txt
EMBEDDING_PROVIDER=sentence-transformers
python backend/manage.py embed      # re-embed under the new provider
```

Three things this design gets right on purpose:

- **Similarity thresholds are per provider, not global.** A cutoff tuned for a transformer
  would reject every genuine match from the lexical embedder. Each provider carries its own
  default; `SIMILARITY_THRESHOLD` overrides it only if you set it deliberately.
- **A provider change invalidates its vectors.** Rows record the provider that wrote them, and
  a vector from a different embedding space is treated as stale and re-embedded rather than
  silently compared against the new ones.
- **Similarity never redefines the statistical sample.** Comparable events are still chosen by
  event type and ticker/sector. Similarity is *annotation*. If a tunable threshold could add
  or drop events, the sample size — the number the whole signal is gated on — would move
  whenever someone nudged a config value.

Similarity search is bounded by the subject event's timestamp in SQL, for the same reason the
historical statistics are: a neighbour search that can see later events leaks the future.

## Event studies

The Ticker page runs a proper market-model event study on demand. Where the signal's historical
statistics subtract the benchmark's raw return (implicitly assuming β = 1), the event study
estimates α and β per ticker:

```
R_i,t = α + β·R_m,t + ε        estimated over 180 trading days,
                                ending 5 sessions before the event
AR_t  = R_i,t − (α + β·R_m,t)
CAR   = Σ AR over the event window
```

Windows reported: `[0,0]`, `[0,+1]`, `[0,+4]`, `[−1,+1]`, `[0,+19]`, each with the cumulative
average abnormal return across events, the median, the positive share, and a t-statistic.

The honesty rules are in the code, not the caption:

- The estimation window ends **before** the event, with a gap, so the run-up cannot contaminate
  α and β.
- An event whose estimation window has fewer than 60 usable observations is **skipped and
  listed**, not quietly dropped — the sample stays auditable.
- An incomplete event window returns nothing rather than a partial CAR presented as a full one.
- A cross-sectional t-statistic is withheld below N=5 and shown as "—".
- The UI says explicitly that on samples this size "not significant" means *no detectable
  effect*, not *no effect*.

The mock market data generates each equity as `β·market + idiosyncratic noise`, so β is
recoverable and the event study is exercised meaningfully offline rather than against
uncorrelated random walks.

---

## Return conventions

A return is never a bare number; it always carries the basis it was computed on.

- **Reaction day** = the first session that can price the event in. An event during regular hours
  reacts that session; an event at 2 a.m., after the close, or at the weekend maps to the **next
  session open** and is labelled `next_open`.
- **Daily baseline** = the close *before* the reaction day, so the reaction day's own move is
  inside the `1d` return. `3d` and `5d` are three and five sessions counting the reaction day.
- **Abnormal return** = raw return − benchmark return (SPY), and separately − sector ETF return
  where the ticker is mapped to one.
- **Intraday horizons (1/5/15/30/60 min) are computed only if the configured provider actually
  returns intraday history for that date.** A horizon crossing the closing bell is omitted rather
  than stretched into the next session. With the default free provider there is no intraday data,
  so only 1d/3d/5d appear.

US market hours, pre/after-market, weekends and the NYSE holiday calendar (including Good Friday
and the 13:00 early closes) are handled in `app/market/calendar.py` and covered by tests.

---

## Backtesting

```
POST /api/backtest
{ "start": "2024-01-01", "end": "2026-09-01", "ticker": null, "event_type": null,
  "min_signal": 0.2, "holding_days": 5, "sentiment_mode": "rule_based" }
```

Each event in the window is scored **as of its own timestamp**. `build_comparable_set` is called
with the event's `source_timestamp` as the upper bound and `persist=False`, so a backtest can
neither see the future nor write into the live signal tables. The observation is the price change
over `holding_days` sessions from the reaction day, aligned to the signal's direction.

**`sentiment_mode`**

| Mode | What it uses | Contamination |
|---|---|---|
| `rule_based` *(default)* | A fixed lexicon. No model call, no network. | None. This is the clean comparison. |
| `llm` | The stored model analysis for each event. | **Labelled contaminated.** The model may already know what followed. Treat it as an upper bound, not a measurement. |

**Splits are chronological.** The window is cut into train (first 60%), validation (next 20%) and
test (last 20%) *by date*. Random k-fold on a time series lets the future inform the past and is
never offered, not even as an option.

**What the numbers mean — and do not mean.** Mean, median, win rate, SD, max drawdown (on the
cumulative sum of observations), best and worst are reported per split. Sharpe is annualised but
**withheld below 20 observations**. `overlap_fraction` reports how many holding windows overlap a
previous one; overlapping windows are correlated, so effective N is smaller than N and apparent
significance is inflated. Nothing here models costs, spread, slippage, position sizing or capital
— these are signal-aligned price changes, not returns on a portfolio.

Runs are stored, so `GET /api/backtest/runs` gives you the history and
`GET /api/backtest/runs/{id}` the detail. Against the default mock market provider, a backtest
measures the mock provider and nothing else.

---

## Data sources

| Source | Status | Notes |
|---|---|---|
| **Mock** | ✅ working | Default for local dev. No network. |
| **WhiteHouse.gov** | ✅ implemented | RSS with a candidate-URL list; feed paths move between administrations, so the list is configurable. |
| **Federal Register** | ✅ implemented | Official, free, **no API key**. The authoritative record for executive orders and proclamations. Lags the press release by hours to days, so it backstops rather than races. |
| **News RSS** | ✅ implemented | Configurable feeds. **Headline, publication, author, timestamp, URL and a short summary only** — full article text is never stored, and the truncation is enforced at ingestion. |
| **Truth Social** | ⛔ **manual import only** | No official public API, and the terms of service prohibit unauthorised automated access. We ship the adapter interface, a mock, and a CSV/JSON importer — and refuse to scrape. See [`docs/PHASE0_FEASIBILITY.md`](docs/PHASE0_FEASIBILITY.md) §1.1. |
| **Congress.gov** | ✅ implemented | Official free API. Needs a free `CONGRESS_API_KEY`; without one the adapter reports `NEEDS_KEY` and polls nothing rather than erroring. A bill's *latest action date* is part of its identity, so a bill that moves is a new event rather than a duplicate of its introduction. |
| **OGE** | ⛔ **manual import only** | OGE publishes filings as documents, not as an API. The adapter stores only what a filing itself lists — filing date, individual, form type, listed entities, source URL, document reference — and carries a disclaimer field. There is **no** value, share-count or position field anywhere in the payload, so an undisclosed holding cannot be inferred even by accident. Point `OGE_FEED_URL` at a JSON index you maintain, or use the manual import. |

Enable sources with `ENABLED_SOURCES=mock,whitehouse,federal_register,news_rss,congress`.

A source that is implemented but unconfigured reports its own state rather than failing:
`NEEDS_KEY` (Congress without a key) and `MANUAL_ONLY` (Truth Social, OGE) are configuration
states, not errors — they never count towards the failure threshold and never raise a degraded-
source alert.

**One failing source never affects another.** Each adapter runs in isolation, records its own
health row (ONLINE / DEGRADED / ERROR with a failure counter), and repeated failures raise a
system notification and show as degraded in the UI.

### Importing an archive

```bash
# JSON or CSV. Accepts id/post_id, created_at/timestamp, text/content.
cd backend && python manage.py import /path/to/posts.json --source truth_social
```

Or `POST /api/admin/import` with `{"source": "truth_social", "rows": [...]}`. Re-importing the
same file inserts nothing.

---

## Configuration

All configuration is environment variables; see [`.env.example`](.env.example) for the annotated
list. **Secrets are read only from the environment and never reach the browser** — the sole value
in the public `/api/config` endpoint is the Web Push *public* key, which is public by design.

Key choices:

- `MARKET_DATA_PROVIDER` — `mock` (synthetic, deterministic, supports intraday) or `stooq` (free,
  no key, **daily bars only**).
- `LLM_FAKE_MODE=true` — use the offline canned scorer instead of the API. Results are stamped
  with the model name `canned-mock` everywhere they appear, so a canned reading is never mistaken
  for a real one.
- `ANTHROPIC_ANALYSIS_MODEL` — defaults to `claude-opus-5`. Switching to `claude-sonnet-5` cuts
  the analysis bill by roughly 60% at the same traffic.

---

## Digests

A digest is a scheduled summary: top bullish and bearish signals, watchlist events, source health,
notification counts and unresolved delivery failures, with the "association is not causation"
note attached.

It fires at **each user's own local digest hour** (`digest_hour_local`, in their `timezone`), not
at a fixed UTC hour, so the schema stays correct if a second user in another timezone is ever
added. The worker checks every hour and sends only what is due.

Two deliberate exceptions: a digest **ignores quiet hours and the per-hour rate limit**. Both of
those exist to suppress unsolicited interruptions, and a digest the user scheduled is not one.

Idempotency is per local *day* (per local *hour* for the opt-in `HOURLY_DIGEST_ENABLED` cadence),
so a worker restart or a manual `POST /api/digest/send` cannot send the same digest twice.
`GET /api/digest/preview` renders it without sending.

Delivery goes to the in-app notification centre first — that is the system of record — and to
email and push only if the user has enabled those channels. **An email or push failure never
loses the digest.**

---

## Deployment

**Recommendation: one small always-on VPS running `docker compose`** — e.g. Hetzner CX22
(2 vCPU, 4 GB, ~$4.50/month).

Why, in short: the workload is a **polling worker that must never sleep**, which disqualifies
scale-to-zero free tiers on requirements rather than on price; app + worker + Postgres on one box
comes in at roughly $6/month all-in, where managed equivalents run $21+; and Phase 2's local
embedding model wants ~1 GB of RAM that a 256 MB managed instance does not have. The trade-off is
that you own OS patching and backups. The best managed alternative inside budget is Fly.io with
auto-stop **disabled** (~$12–14/month), at the cost of tighter memory for Phase 2.

**Postgres needs pgvector.** The compose file uses the `pgvector/pgvector:pg16` image, so this
is already handled; on a hand-rolled install add `postgresql-16-pgvector` before migrating.

```bash
# On the server
git clone <your-repo> && cd Trump-trading-
cp .env.example .env
# Edit .env: set a real APP_AUTH_TOKEN, ENVIRONMENT=production, and
# ENABLED_SOURCES=whitehouse,federal_register,news_rss
# and MARKET_DATA_PROVIDER=stooq
docker compose up -d --build
docker compose logs -f worker
```

Put a TLS terminator in front (Caddy is two lines; nginx + certbot works too). **HTTPS is not
optional** — service workers and the Push API only run on a secure origin, so without TLS the PWA
will not install and push cannot work at all.

Back up with `docker compose exec db pg_dump -U postgres trumpmarket | gzip > backup.sql.gz` on a
cron.

### Installing on iPhone

1. Open the site in **Safari** (not Chrome — and on iOS, "Chrome" is WebKit anyway).
2. Tap **Share** → **Add to Home Screen** → **Add**.
3. Open the app **from the Home Screen icon**, not from a Safari tab.
4. Go to **Settings → Enable notifications** and allow when prompted.

Step 3 is not a formality: **iOS delivers Web Push only to an installed web app.** In a Safari
tab the subscription cannot be created and no push will ever arrive. Settings shows
"Installed to Home Screen: yes/no" so you can check.

---

## API key setup

| Key | Needed for | How to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Real relevance triage and full analysis. Without it, events still flow and the signal uses rule-based sentiment. | <https://console.anthropic.com> |
| `CONGRESS_API_KEY` | Congress.gov source (Phase 3). | Free key from <https://api.data.gov/signup/> |
| `MARKET_DATA_API_KEY` | Only if you swap in a provider that requires one (Alpha Vantage, Twelve Data). Stooq and the mock provider need nothing. | Provider's site |
| `NEWS_API_KEY` | Optional paid news API. RSS needs no key. | Provider's site |

### Web Push (VAPID) setup

Push delivery is live. Generate a keypair and restart:

```bash
make vapid            # or: python scripts/generate_vapid_keys.py
```

Put the printed values in `WEB_PUSH_PUBLIC_KEY`, `WEB_PUSH_PRIVATE_KEY` and `WEB_PUSH_SUBJECT`
(e.g. `mailto:you@example.com`). **Only the public key is ever sent to the browser.** Then
re-enable notifications in Settings so a subscription is created against the new key — rotating
the key invalidates every existing subscription, so each device has to opt in once more.

With no keys set, push disables itself cleanly: every attempt records an `UNAVAILABLE` delivery
row explaining why, and the in-app notification centre carries on unaffected.

How failures are handled, because this is where push implementations usually go wrong:

| Push service says | What happens |
|---|---|
| `201` | Delivery marked SENT; the subscription's failure counter resets. |
| `404` / `410` | The endpoint is permanently gone. The subscription is deactivated immediately and never retried; after 30 days it is deleted. |
| `429`, `5xx`, timeouts | Retryable. The delivery is rescheduled with exponential backoff, up to `WEB_PUSH_MAX_RETRIES`, by the worker's `push_retry` job. |
| Anything else | Permanent failure, logged with the provider's own response text. |
| Provider raises | Caught. Notification delivery never propagates into the pipeline. |

A subscription that fails `WEB_PUSH_MAX_FAILURES` times in a row is retired, so one dead endpoint
cannot consume the retry budget forever.

### SMTP (optional)

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `EMAIL_FROM`. **Email disables
itself automatically when `SMTP_HOST` or `EMAIL_FROM` is missing** — there is no half-enabled
state. Send failures are logged to `email_delivery_logs` and never block event processing.

---

## Notification troubleshooting

Work down this list; each step rules out the one below it.

| Symptom | Check |
|---|---|
| **Nothing at all, not even in-app** | Settings → is "Notifications enabled" on? Are you inside quiet hours (Settings shows this live)? Has the hourly cap been hit? Press "Send test notification": it bypasses quiet hours and the rate limit, so if *that* does not appear, the problem is the worker or the API, not notifications. |
| **In-app works, push does not** | Settings → "Push configured" must be yes (VAPID keys set) **and** "Installed to Home Screen" must be yes on iOS. Both are shown on the page. |
| **iPhone shows no permission prompt** | The prompt only appears from a real tap, and only in an installed web app. Re-add to the Home Screen and tap "Enable notifications" again. |
| **Push worked, then stopped** | The push service expires subscriptions. A 404/410 marks the subscription inactive — visible in Settings → Push subscriptions and in the data-quality view. Re-enable notifications to create a fresh one. |
| **A push failed once and never arrived** | Retryable failures are rescheduled automatically by the worker's `push_retry` job (every 2 minutes). Settings → Operations → "Retry failed push" forces a pass now. |
| **Similar events list is empty or odd** | Check Settings → the similarity measure. The default embedder is *lexical*: it matches shared wording, not meaning. Switch to `sentence-transformers` for semantic matching. |
| **Alerts fire once, then never again** | That is the re-arm rule working: a threshold alert fires on *crossing*, then waits for the signal to return inside the band. Set `repeat_alerts` on the rule if you want repeats. |
| **Too many alerts** | Raise `min_sample_size` and `min_confidence` on the rule; the seeded threshold rule already defaults to N≥10 and confidence≥0.3. Lower `max_per_hour`. |
| **No events at all** | Settings → Sources. A source in ERROR shows its last error. Check `docker compose logs worker`, and `/api/admin/data-quality` for unprocessed rows. |
| **Signals all say "unreliable"** | You have fewer than 10 comparable past events. Import a larger archive — that is the sample the statistics are computed from. |

---

## Testing

```bash
make test            # or: cd backend && python -m pytest -q
```

**368 tests, all passing.** They run against a real Postgres with pgvector (`trumpmarket_test`
by default; override with `TEST_DATABASE_URL`) because the idempotency guarantees are enforced by
database constraints and the similarity search is real SQL — testing either against a fake would
test nothing. The suite drops every table it touches, so it refuses to start unless the database
name contains `test`.

Coverage maps to the spec's list: deduplication · ticker matching and confidence · timestamp and
market-hours handling · JSON validation and the retry · return and abnormal-return calculation ·
sample-size flags · signal calculation · look-ahead prevention · retry and backoff · rule
evaluation · threshold crossing and re-arming · notification dedup · quiet hours · digest
generation · push subscription register and expiry cleanup · email failure handling ·
push-unavailable fallback · API auth and validation.

Phase 2 adds: vector storage and staleness detection · similarity ranking and its time bound ·
novelty windows and the lexical fallback · VAPID status-code handling (201 / 404 / 410 / 429 /
5xx / raised) · retry scheduling, budget exhaustion and subscription retirement · OLS market-model
recovery of a known β · abnormal returns isolating a known shock · an event study that finds a
planted effect *and* correctly finds nothing when there is nothing.

Phase 3 adds: point-in-time signal recomputation · chronological splits that never shuffle ·
max drawdown and the Sharpe sample floor · overlapping-window detection · the rule-based /
LLM contamination label · digest scheduling in the user's own timezone · digest idempotency
across local days and cadences · digests overriding quiet hours and rate limits by design ·
Congress bill identity and date parsing · the OGE payload's *absence* of any value field ·
`NEEDS_KEY` / `MANUAL_ONLY` reporting · search-to-alert parity (an alert built from a search
fires on exactly the events that search returns).

The **end-to-end test** (`tests/test_e2e.py`) does exactly what the spec asks: inserts a mock
event, runs the pipeline, generates a signal, matches a watchlist rule, creates an in-app
notification, attempts a mock push delivery, then re-runs the worker and asserts that no
duplicate event, signal, notification or delivery exists.

### Mock dataset

| File | Contents |
|---|---|
| `backend/app/seed/mock_events.json` | 12 recent items, including a deliberate republish (exercises dedup), a generic "apple pie" mention (exercises LOW-confidence ticker matching), and a golf post (exercises the relevance filter). |
| `backend/app/seed/archive_events.json` | 286 synthetic past events spanning 2024–2026, sized so all three sample bands (ok / limited / unreliable) are visible. Regenerate with `scripts/generate_archive.py`. |
| `backend/app/seed/canned_analyses.json` | Canned analyses used by `LLM_FAKE_MODE`. |
| `backend/app/seed/tickers.json`, `aliases.json` | 32 instruments and 41 aliases, including the deliberately ambiguous ones. |

Market prices are generated deterministically by the mock provider — same bars on every machine,
every run.

---

## Phase status

### Phase 1 — complete

**Works:** single-user auth · PostgreSQL with Alembic migrations · background worker with
per-source scheduling and Postgres advisory locks · mock / WhiteHouse / Federal Register / news
RSS adapters · the full pipeline · market-data abstraction with mock and Stooq providers ·
historical statistics and signal calculation with the mandatory "Why?" panel · all seven pages ·
in-app notifications with rule evaluation, threshold crossing, re-arming and deduplication · the
PWA shell (manifest, service worker, installable on iPhone) · the test suite and the end-to-end
test.

**Mocked:** market data (`mock` provider) and LLM analysis (`canned-mock`) by default in local
development. Both are one environment variable away from real.

**Needs a key:** real analysis (`ANTHROPIC_API_KEY`). Nothing else — the app is fully functional
with zero keys.

### Phase 2 — complete

**Works:** Web Push delivery over VAPID with per-status handling, backoff retries, subscription
retirement and expiry cleanup · pgvector embeddings with an HNSW cosine index · similarity search
bounded against look-ahead · embedding-based novelty with a lexical fallback · a "similar past
events" list with subsequent returns · market-model event studies with CAAR and t-statistics ·
`embed` and `push_retry` worker jobs · admin endpoints for both.

**Mocked / limited:** the default embedder is **lexical, not semantic** — real semantic
embeddings are one env var and one `pip install` away, and the UI says which is active. The
`sentence-transformers` path could not be executed in the build environment (no network access to
the model host), so it is written and type-checked but **not run**; the hashing path is fully
tested.

**Truth Social:** still manual-import only. No terms-compliant public feed was identified, so
nothing changed — see [`docs/PHASE0_FEASIBILITY.md`](docs/PHASE0_FEASIBILITY.md) §1.1.

### Phase 3 — complete

**Works:** a real backtest engine (`POST /api/backtest`) that recomputes every signal
point-in-time, splits observations chronologically into train / validation / test, and reports
mean, median, win rate, SD, max drawdown, Sharpe, best/worst and overlap fraction per split ·
stored runs (`GET /api/backtest/runs`) · scheduled daily digests that fire at each user's own
local digest hour over in-app, email and push · the Congress.gov adapter · the OGE manual-import
adapter · search-to-alert parity, so an alert created from a search fires on exactly the events
that search returns · the data-quality and cost page in the UI.

**Backtest honesty rules, all enforced in code:**

- **Rule-based sentiment is the default mode and involves no language model at all.** An
  LLM-scored run is available and carries a visible *"potentially contaminated"* banner, because
  the model's training data may already contain knowledge of what followed those events.
- Splits are **chronological, never random** — shuffling time-series observations into random
  folds lets the future inform the past.
- Every signal is recomputed as of the event's own timestamp: comparable events, similarity and
  novelty are all bounded by it, and the backtest writes nothing to the live signal tables.
- Overlapping holding windows are counted and reported, because they inflate apparent
  significance.
- Sharpe is withheld below 20 observations rather than printed from a sample too small to mean
  anything.
- No costs, spread, slippage, position sizing or capital are modelled. These are signal-aligned
  price changes, not returns on a portfolio, and the results page says so.

**Digests:** the daily digest is something the user asked for, so it deliberately **overrides
quiet hours and the hourly rate limit** — both exist to suppress interruptions, not scheduled
summaries. It is idempotent per local day (and per local hour for the opt-in hourly cadence), so
a worker restart cannot send it twice. `NEEDS_KEY` and `MANUAL_ONLY` sources are excluded from
the digest's "degraded sources" line: an unconfigured source is not a broken one.

**Needs a key:** Congress.gov needs a free `CONGRESS_API_KEY`. Email digests need SMTP; push
needs VAPID keys. Without any of them the in-app notification centre still delivers the digest.

**Not implementable reliably:** OGE has no API, so it is manual-import (or a JSON index you
maintain) rather than a poller — the same position as Truth Social, for a different reason.

---

## Cost

| Item | Estimate |
|---|---|
| VPS (Hetzner CX22) | ~$4.50/month |
| Backups + domain | ~$2/month |
| Market data (Stooq) | $0 |
| Web Push (self-hosted VAPID) | $0 |
| Embeddings (local, either provider) | $0 |
| Anthropic API | ~$1–19/month depending on volume and model |
| **Total** | **~$8–25/month** |

The LLM figure is the one that moves. At ~200 raw items/day, the rule filter drops ~70% before
any model call, ~60 reach Haiku triage and ~20 survive to full analysis: roughly $19/month on
`claude-opus-5`, or ~$6/month on `claude-sonnet-5`. Cost controls in place: dedup before any call,
a rule-based gate, cheap-model triage, a **permanent content-hash cache** so identical text is
never analysed twice, per-call token logging, and a hard `LLM_DAILY_CALL_BUDGET`. Current spend is
visible at `/api/admin/data-quality`. Verify rates at <https://claude.com/pricing> — the estimates
above are from published prices at time of writing.

---

## Known limitations

These are real and they do not have workarounds:

1. **Truth Social has no legitimate feed.** The fastest-possible-reaction use case is not
   achievable through permitted means. News RSS covers most market-relevant posts indirectly,
   1–15 minutes later, attributed to the publication rather than the post.
2. **Free market data is daily-only.** Intraday horizons are implemented but stay off unless you
   pay for a provider that supplies intraday history.
3. **Small samples.** A specific company rarely has 20+ comparable past events. Most signals will
   honestly say "unreliable", and the score gives those statistics zero weight.
4. **Events are not independent.** Comparable events cluster in time and share causes, so the
   effective sample is smaller than N suggests. The panel says so on every signal.
5. **Regime change.** The sample spans different policy, rate and market regimes than the present.
6. **iOS push is best-effort.** Requires Home Screen installation and a recent iOS; delivery goes
   through APNs and can be delayed or dropped by Low Power Mode, Focus modes or connectivity. A
   PWA on iOS **cannot poll in the background at all** — which is precisely why the worker is
   server-side. The in-app notification centre is the system of record; push is a convenience.
7. **Unverified endpoints.** The build environment had no outbound network access to government
   or market sites, so no live feed URL in this repository has been confirmed against the real
   service. Every URL is configurable and every adapter fails into the health table by design —
   but check Settings → Sources on first deployment. See `docs/PHASE0_FEASIBILITY.md`.
8. **LLM contamination in backtests.** A model's training data may contain knowledge of what
   followed a historical event, so an LLM-scored backtest can look predictive for reasons that
   have nothing to do with the signal. Backtesting therefore defaults to rule-based sentiment,
   which involves no model at all, and every LLM-scored run carries a "potentially contaminated"
   banner. There is no way to remove the contamination — only to label it and offer the clean
   comparison.
9. **The default embedder is lexical.** It matches shared wording, not meaning, so two
   differently-worded statements about the same policy will look unrelated to it. Semantic
   embeddings are one env var away but cost ~2 GB of dependencies. The UI names the active
   measure on every similarity score so the two are never confused.
10. **The `sentence-transformers` path is unexecuted.** The build environment had no network
   access to the model host, so that provider is written and type-checked but has never been
   run. Expect to `pip install`, run `manage.py embed`, and check the first result before
   trusting it.
11. **Event studies on political events rarely reach significance.** With a few dozen comparable
   events and daily bars, the honest answer is usually "no detectable effect". The UI says so
   rather than dressing up a t-statistic of 0.4.
12. **Backtest results are not portfolio results.** No costs, spread, slippage, position sizing
   or capital are modelled, holding windows overlap, and a backtest run over the mock provider
   measures the mock provider. The numbers are signal-aligned price changes and nothing more.
13. **OGE is manual.** There is no OGE API to poll, so filings arrive by import. The adapter is
   deliberately built so that undisclosed holdings *cannot* be represented — there is no value,
   share or position field to put them in.

---

## Repository layout

```
backend/
  app/
    api/            FastAPI routes (feed, user, admin) + serialisers
    embeddings/     provider interface, hashing + sentence-transformers, pgvector search
    llm/            Anthropic client, prompts, schema, offline canned client
    market/         provider interface, mock + Stooq, US market calendar, returns
    pipeline/       relevance, ticker matching, analysis, historical, signals,
                    event studies, notifications, web push, email, digests,
                    backtesting, pipeline runner
    seed/           mock dataset, sample archive, loader and archive importer
    sources/        adapter interface, adapters, HTTP retry/backoff, registry
    worker/         APScheduler worker
    config.py  db.py  models.py  schemas.py  auth.py  main.py
  alembic/          migrations
  tests/            368 tests, including the end-to-end test
  manage.py         operational CLI (seed, import, poll, pipeline, embed, status)
frontend/
  src/              React + TypeScript pages, components, API client
  public/           manifest, service worker, icons
docs/
  PHASE0_FEASIBILITY.md
scripts/            archive, icon and VAPID key generators
```

---

*Research and information tool. Not investment advice. No brokerage integration, no order
placement, nothing autonomous.*
