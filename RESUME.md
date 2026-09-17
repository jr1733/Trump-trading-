# RESUME — where this is and what to do next

Written for whoever picks this up next, including me in three months. The README explains how the
system works; this file says what state it is actually in, what is untrustworthy, and what to do
first.

**Branch:** `claude/trump-market-intelligence-pwa-nctq2d` · **Phases 0–3 complete** ·
**394 tests passing** · frontend builds clean · pyflakes clean.

---

## Start here (five minutes)

```bash
cp .env.example .env
docker compose up --build         # http://localhost:8080
```

Sign in with `APP_AUTH_TOKEN` from `.env`. You get the mock source, synthetic prices, canned
analyses and a 286-event archive. Everything in the UI is live; nothing needs a key.

You will see a **DEMO DATA** banner on every page. That is correct and it goes away by itself
once you configure a real provider — there is no flag to remember to unset.

---

## What is real and what is not

| | State |
|---|---|
| Pipeline, dedup, idempotency, signals, alerts, digests, backtests | **Real.** Exercised by 394 tests against a real Postgres. |
| Market prices | **Synthetic** by default. Stooq provider written, **never executed against the live service.** |
| LLM analysis | **Canned** by default (`LLM_FAKE_MODE=true`). Real client written and unit-tested against a fake SDK. |
| Feed URLs (WhiteHouse, Federal Register, news RSS) | **Unverified guesses.** |
| Stooq symbol mappings (`VIX`→`^vix`, `TNX`→`10yusy.b`) | **Unverified guesses.** |
| `sentence-transformers` embedding path | **Written, type-checked, never run** (no network to the model host). |
| Congress.gov adapter | Written and unit-tested. Never called against the live API. |
| OGE | Manual import only. There is no API and there will not be one. |

The honest summary: **every line of business logic is tested; almost no line of network I/O has
met the internet.** The build environment denied outbound CONNECT to every government and market
host, so this could not be fixed from here.

---

## Do these first, in this order

### 1. Verify the endpoints (15 minutes, do it before anything else)

```bash
make verify-sources ARGS="--all-sources -v"
MARKET_DATA_PROVIDER=stooq make verify-sources ARGS="--market-only"
```

Expect some of this to fail. That is the point of the script; it is not a sign the deploy is
broken. Specifically:

- **`EMPTY` is the dangerous state**, not `FAILED`. It means the URL still serves *something*
  that is not a feed — the failure that otherwise hides for weeks. Fix it by pointing
  `WHITEHOUSE_FEEDS` / `NEWS_RSS_FEEDS` at whatever the current path is.
- If a Stooq symbol fails, the mapping is in `backend/app/market/stooq_provider.py`
  (`_SYMBOL_OVERRIDES`). `VIX` and `TNX` are the two most likely to be wrong; ordinary equities
  use a mechanical `.us` suffix and should be fine.
- `NEEDS KEY` (Congress) and `MANUAL ONLY` (Truth Social, OGE) are configuration states. They are
  not failures and do not fail the run.

Do not skip this. Until it passes, every number in the app is either synthetic or absent.

### 2. Sanity-check one real signal end to end

Once a real source and real prices are flowing, open one ticker and read its **Why?** panel all
the way down. You are checking that the components are plausible, the sample size is what you
expect, and the horizon labels match what the provider can actually supply (Stooq = daily only,
so 1d/3d/5d and nothing shorter).

### 3. Decide whether the lexicon is good enough

`backend/app/pipeline/relevance.py` holds both the relevance gate and the rule-based sentiment
lexicon. Both were written by hand and neither has been evaluated against labelled data. The
relevance gate is the bigger cost lever — it decides what reaches a model — and
`/data-quality` now shows per-source volume and attributed spend so you can see it working or
not. If Congress is producing many events and many model calls, raise
`CONGRESS_RELEVANCE_THRESHOLD`.

---

## Known-sharp edges

- **Backtest numbers on mock data measure the mock data.** They are not a finding. The demo
  figures quoted in the README (N=87 → 40 under non-overlapping mode) are there to show the
  mechanism, not a result.
- **The rule-based lexicon carries hindsight.** It was written in 2026 knowing which topics moved
  markets over the scored period. It is the *cleaner* backtest mode, not a clean one. The UI says
  so; do not let that slip in any write-up.
- **Test/train disagreement on the demo data is severe** (train −0.72%, test +2.11%). At N=42 and
  N=7 neither means anything. Do not quote a single headline figure.
- **The default embedder is lexical, not semantic.** Two differently-worded statements about the
  same policy look unrelated to it. `EMBEDDING_PROVIDER=sentence-transformers` fixes that for
  ~2 GB of dependencies and a `manage.py embed` re-run — and that path has never been executed.
- **`min_sample_size` on an alert rule can raise the sample bar but not lower it.**
  `ALERTS_REQUIRE_USABLE_SAMPLE=true` is a hard floor. If alerts seem too quiet, that is why.
- **The demo database's signals are recomputed, not migrated.** If you change scoring logic,
  existing `signals` rows keep their old scores until the pipeline reprocesses those events.

---

## Where things live

| I want to… | Look at |
|---|---|
| change how a score is built | `backend/app/pipeline/signals.py` |
| change what reaches a model | `backend/app/pipeline/relevance.py`, `runner.py` (`triage_relevance_floor`) |
| add a source | `backend/app/sources/` — subclass `SourceAdapter`, register in `registry.py` |
| change backtest mechanics | `backend/app/pipeline/backtest.py` |
| change digest content or timing | `backend/app/pipeline/digest.py` |
| fix a feed URL | `.env` (`WHITEHOUSE_FEEDS`, `NEWS_RSS_FEEDS`) — no code change needed |
| fix a market symbol | `backend/app/market/stooq_provider.py` |
| see what it is costing | `/data-quality` in the UI, or `GET /api/admin/data-quality` |

---

## Not built, and why

- **Intraday horizons.** Implemented but inert: no free provider supplies intraday history. They
  switch on by themselves if you configure one that does.
- **Truth Social polling.** No terms-compliant public feed exists. Manual import only. This is a
  deliberate refusal, not a gap to fill later — see `docs/PHASE0_FEASIBILITY.md` §1.1.
- **OGE polling.** No API exists. Manual import, or point `OGE_FEED_URL` at a JSON index you
  maintain.
- **Any form of automatic weight fitting.** Nothing in the codebase is fitted; the splits exist
  as a discipline for *manual* tuning. If you add an optimiser, fit on train only and report test
  once — the README's backtesting section explains the protocol.
- **Multi-user anything.** The schema is user-scoped (`user_id` everywhere) but auth is a single
  bearer token. Adding real users means adding real auth, not migrating the schema.

---

## Next things worth doing

1. **Verify endpoints and fix what is broken** (above). Nothing else matters until this is done.
2. **Run a week on real data with `LLM_FAKE_MODE=true`.** Confirms polling, dedup and the
   relevance gate under real volume without spending a cent on tokens.
3. **Then turn on the model** and watch `/data-quality` for the first few days. `LLM_DAILY_CALL_BUDGET`
   is the hard stop; the per-source table is how you find out which source is spending it.
4. **Label ~100 events by hand** and measure the relevance gate's precision and recall against
   them. Right now its thresholds are guesses that have never been scored.
5. **Re-run the backtest on real prices** with `non_overlapping_only=true`, and treat whatever it
   says as the first honest number this tool has produced.
