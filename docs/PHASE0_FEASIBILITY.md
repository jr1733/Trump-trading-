# Phase 0 — Feasibility Report

**Date:** 2026-09-16
**Scope:** source availability, iPhone PWA/Web Push behaviour, hosting within ~$20/month.

> **Verification caveat — read this first.** The build sandbox this report was written in
> has outbound network egress restricted to package registries (PyPI/npm) and the Anthropic
> API. Every attempt to reach `whitehouse.gov`, `federalregister.gov`, `api.congress.gov`,
> `stooq.com`, etc. returned `403` from the egress proxy. So the endpoint-level claims below
> are **from prior knowledge, not live probes**, and government site structure changes
> frequently. Consequently *every* feed URL, API base URL and provider choice in this project
> is configurable (env var or the `sources` DB table), no adapter hard-codes a URL it cannot
> survive losing, and each adapter fails independently into the `source_health` table.
> Confirm each URL once on first real deployment; the Settings → Sources page shows which
> ones are answering.

---

## 1. Source-by-source feasibility

### 1.1 Truth Social — ❌ no legitimate programmatic feed

| | |
|---|---|
| Official public API | None. |
| Free/paid | N/A |
| Terms | The Truth Social terms of service prohibit unauthorised automated access, scraping and crawling. The platform runs a Mastodon-derived backend, and Mastodon-shaped endpoints have at various times been reachable without auth — but reachability is not permission, and relying on it is both a ToS problem and operationally fragile. |
| **Decision** | **Do not scrape.** Ship the adapter *interface*, a mock adapter, and a manual CSV/JSON importer. Document the gap prominently in the UI (Settings → Sources shows Truth Social as `MANUAL_ONLY`). |
| Practical fallback | High-salience posts are re-reported within minutes by wire services, so the **news RSS adapter covers most market-relevant content indirectly**, with a delay of roughly 1–15 minutes and an attribution loss (the event is stored as a news item that quotes the post, not as the post). |
| Phase | Interface + mock + importer in Phase 1; a real adapter only if a licensed/permitted feed is identified. |

**What this means for the product:** the "fastest possible reaction to a post" use case is not
achievable through legitimate means. This is a structural limitation, not an implementation gap,
and the README says so.

### 1.2 WhiteHouse.gov — ✅ workable, with a stronger official fallback

- **Primary:** the site has historically exposed WordPress-style RSS at `/feed/` and per-section
  feeds (e.g. `/presidential-actions/feed/`). Free, no key. **Fragile:** each administration
  rebuilds the site and feed paths move or disappear. The adapter therefore takes a *list* of
  candidate feed URLs and uses the first that parses.
- **Fallback (and arguably the better primary): the Federal Register API** —
  `https://www.federalregister.gov/api/v1/documents.json`, free, **no API key**, documented JSON,
  and it is the *authoritative* publication channel for executive orders, proclamations and
  presidential memoranda. Filter with
  `conditions[presidential_document_type][]=executive_order|proclamation|determination|memorandum`.
  Trade-off: Federal Register publication **lags** the White House press release by hours to a
  couple of days, so it is a completeness backstop, not a latency source.
- **Decision:** implement both in Phase 1. WhiteHouse RSS for latency, Federal Register for
  authority and gap-filling. Duplicate coverage is handled by the dedup stage.

### 1.3 Congress.gov — ✅ official free API

- `https://api.congress.gov/v3/`, key issued free via api.data.gov, documented rate limit of
  ~5,000 requests/hour. Supports bills, actions, subjects, sponsors and committees; suits both
  "new bills" and "status changes" if polled with `fromDateTime`/`toDateTime`.
- **Decision:** Phase 3 as specified. Key is optional; the adapter disables itself cleanly when
  `CONGRESS_API_KEY` is unset.

### 1.4 OGE — ⚠️ public documents, no usable API

- Public financial disclosure filings for executive-branch officials are published as documents
  (OGE Form 278e etc.), searchable through public portals; there is **no bulk/JSON API**, and
  much of the content is scanned or semi-structured PDF.
- **Decision:** Phase 3, and deliberately minimal: store only filing date, individual, filing
  type, the entities/assets *as listed on the filing*, source URL and document reference.
  **Never infer undisclosed holdings** — the schema has no field that would let you.

### 1.5 News — ✅ free RSS, with caveats

- Publisher RSS feeds are free and generally permit headline+link syndication. Availability by
  publisher varies and some majors have withdrawn public feeds over time; the feed list is
  therefore a config array, not hard-coded.
- **We store headline, publication, author, timestamp, URL and a short summary only — never full
  article text.** This is both a copyright posture and a storage/cost decision.
- Paid news APIs (NewsAPI and similar) exist; `NEWS_API_KEY` is wired through but optional and
  unused in Phase 1.

### 1.6 Market data — ✅ free daily; ⚠️ intraday needs a key

| Provider | Key | Daily | Intraday | Notes |
|---|---|---|---|---|
| **Mock** (built in) | no | ✅ deterministic | ✅ synthetic | Default for local dev/tests. Seeded PRNG → same bars every run. |
| **Stooq** (`stooq.com` CSV) | **no key** | ✅ | ❌ | Free, no registration, US tickers as `aapl.us`. **Daily only.** Best free default. |
| Alpha Vantage | free key | ✅ | ✅ 1/5/15/30/60min | Free tier is heavily rate-limited (on the order of 25 requests/day) — too tight for backfilling many tickers, usable for a handful. |
| Twelve Data | free key | ✅ | ✅ | ~800 requests/day free; better intraday budget than Alpha Vantage. |
| Yahoo (`yfinance`) | no | ✅ | ✅ | Unofficial/undocumented endpoint, ToS-grey. **Not used.** |

**Answer to the mandated question — which horizons the chosen provider supports:**

> The Phase 1 default (`mock` locally, **`stooq` in production**) supports **daily bars only**.
> Therefore Phase 1 computes the **daily horizons 1d / 3d / 5d** and the UI labels every statistic
> with its horizon basis. The intraday horizons (1/5/15/30/60 min) are implemented behind the
> provider interface's `supports_intraday()` capability flag and stay **switched off** unless a
> provider that actually returns intraday history for the event's date is configured. The mock
> provider reports `supports_intraday() == True`, so the intraday code path is exercised by tests
> even without a paid key.

### 1.7 Historical backfill — ✅ importer, ships with sample data

CSV/JSON importer (`scripts/import_archive.py` + `POST /api/admin/import`) so similarity search
and event studies have prior events. A ~60-event sample archive spanning 2024–2026 with matching
price history ships in `backend/app/seed/`.

---

## 2. iPhone PWA and Web Push — current behaviour

- **Web Push on iOS works, but only for an installed web app.** Since iOS 16.4, Safari supports
  the standard Push API + VAPID — *only* after the user does **Share → Add to Home Screen**.
  A page open in a Safari tab cannot receive push. There is no way to prompt for installation
  programmatically; the README ships step-by-step instructions with this caveat in bold.
- **Permission requires a user gesture.** `Notification.requestPermission()` must be called from
  a tap handler. The Settings page has an explicit "Enable notifications" button for this reason.
- **All iOS browsers use WebKit**, so behaviour is uniform; installing from Chrome on iOS still
  produces a WebKit web app.
- **No background execution.** No Background Sync, no Periodic Background Sync, no background
  fetch. A PWA on iOS cannot poll. **This is why an always-on server-side worker is mandatory**
  rather than a nice-to-have — every schedule, every rule evaluation and every notification
  decision happens server-side, and the phone is a pure client.
- **Delivery is best-effort and can be delayed or dropped** — pushes go through APNs; Low Power
  Mode, Focus modes and connectivity all affect delivery timing. The in-app notification centre
  is therefore the system of record and always-on fallback; push is a convenience layer.
- **Storage eviction:** unused web-app storage can be cleared after roughly a week of non-use.
  Nothing the user cares about is stored only in the browser — all state is server-side.
- **Subscriptions expire.** The server must handle `404`/`410` from the push service by deleting
  the subscription (implemented in Phase 2, table already in the Phase 1 schema).
- Badging on the home-screen icon is partial/unreliable on iOS; the in-app bell badge is the
  reliable unread indicator.

---

## 3. Hosting recommendation (≤ ~$20/month)

**Recommendation: one small always-on VPS running `docker compose` — Hetzner CX22 (2 vCPU,
4 GB RAM, ~€4/month ≈ $4.50) or equivalent.**

Why:

1. **The workload is a polling worker.** It must never sleep. Scale-to-zero free tiers
   (Render free, Fly auto-stop, most "hobby" tiers) are disqualified by the requirement, not by
   price.
2. **Cost.** App + worker + Postgres on one box: ~$4.50/month, leaving the budget for backups
   (~$1) and a domain. Managed alternatives add up fast — Render Starter is ~$7/service and
   ~$7 for Postgres, so app+worker+db ≈ $21 and is already over budget.
3. **Phase 2 needs RAM.** A local `sentence-transformers` model wants roughly 0.5–1 GB resident.
   4 GB comfortably fits Postgres + API + worker + the model; a 256/512 MB managed instance
   does not.
4. **One box = one `docker compose up`**, which is the same command as local dev. Fewer moving
   parts to get wrong.

Trade-off, stated honestly: a VPS means **you** own OS patching, backups and uptime. If that is
unattractive, the best managed alternative inside budget is **Fly.io** — two `shared-cpu-1x`
256 MB machines with auto-stop *disabled* (~$3–4/month each) plus Fly Postgres (~$5) lands around
$12–14/month, at the cost of tighter memory for Phase 2 embeddings.

**Estimated total monthly cost**

| Item | Estimate |
|---|---|
| VPS (Hetzner CX22) | ~$4.50 |
| Backups | ~$1 |
| Domain (amortised) | ~$1 |
| Market data (Stooq) | $0 |
| Web Push (VAPID, self-hosted) | $0 |
| Anthropic API (see below) | ~$1–6 |
| **Total** | **~$8–13/month** |

**LLM cost model.** Current published rates: Haiku 4.5 $1/$5 per million input/output tokens;
Opus 5 $5/$25. Assume ~200 raw items/day, of which the rule filter drops ~70% before any model
call, ~60 reach Haiku triage (~800 in / ~80 out each) and ~20 survive to full Opus analysis
(~1,500 in / ~800 out each):

- Triage: 60 × (800 × $1 + 80 × $5) / 1e6 ≈ **$0.07/day**
- Analysis: 20 × (1,500 × $5 + 800 × $25) / 1e6 ≈ **$0.55/day**
- ≈ **$19/month at that volume.** To stay at the low end of the estimate above, the defaults ship
  with `ANTHROPIC_ANALYSIS_MODEL=claude-opus-5` but the config exposes `claude-sonnet-5`
  ($2/$10 ≈ $6/month for the same traffic) as a one-line swap, and permanent content-hash caching
  means re-processed or duplicate content never costs twice. Verify current rates at
  <https://claude.com/pricing> before relying on these numbers.

---

## 4. Decisions carried into Phase 1

| Ambiguity | Decision |
|---|---|
| Truth Social | Interface + mock + manual import only. Documented gap. |
| WhiteHouse.gov | RSS adapter with a candidate-URL list, **plus** a Federal Register API adapter as the authoritative fallback. |
| Market provider | `mock` default locally, `stooq` in production → **daily horizons only in Phase 1**. |
| Intraday horizons | Implemented, gated behind `provider.supports_intraday()`, off by default. |
| Embeddings / novelty | pgvector + `sentence-transformers` is Phase 2. Phase 1 computes novelty from a lexical (token-set Jaccard) similarity over the trailing 30 days, and labels it as such in the "Why?" panel. |
| Redis | Not used. APScheduler + Postgres advisory locks + a `job_runs` table give scheduling, mutual exclusion and idempotency without another service. |
| Auth | Single shared bearer token from `APP_AUTH_TOKEN`, sent in the `Authorization` header. No cookies → no CSRF surface. |
| Push in Phase 1 | A `NullPushProvider` that records a `notification_deliveries` row with status `UNAVAILABLE`. Real VAPID lands in Phase 2 without changing callers. |

---

*Not a trading system. No brokerage integration, no order placement, no autonomous action.
Statistical association between past events and past price moves is not causation and not a
forecast.*
