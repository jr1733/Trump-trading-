# RESUME — where this is and what to do next

Written for whoever picks this up next, including me in three months. The README explains how the
system works; this file says what state it is actually in, what is untrustworthy, and what to do
first.

**Branch:** `claude/trump-market-intelligence-pwa-nctq2d` · **Phases 0–4 complete** ·
**481 tests passing** · frontend builds clean · pyflakes clean · `make compose-check` passes.

---

## Verified vs unverified

This is the most important table in the file. Everything in the left column has been executed and
its output checked; everything in the right column is written, type-checked and unit-tested
against fakes but **has never met the real thing**, because this build environment denies
outbound CONNECT to every government and market host.

| Verified here | Unverified — check on the VPS |
|---|---|
| Pipeline, dedup, idempotency, signals, alerts, digests, backtests (481 tests, real Postgres) | Every source feed URL (WhiteHouse, Federal Register, news RSS) |
| Migrations 0001–0004 applied to a real database | Stooq's CSV endpoint and its symbol mappings (`VIX`→`^vix`, `TNX`→`10yusy.b`) |
| `pg_dump` → `purge-mock` → `pg_restore` round trip, row counts matched | Alpha Vantage's free tier, its rate limits, and the `VIX`→`VIXY` / `TNX`→`IEF` proxies |
| `docker compose config` — nginx is the only published service | Congress.gov API (written, unit-tested, never called live) |
| Both nginx configs pass `nginx -t` (bar one 1.25+ directive, see below) | Web Push delivery to a real iPhone |
| Analysis-cache regression: proved the old code failed, the new passes | SMTP delivery |
| Haiku effort-gating and pricing regressions | The `sentence-transformers` embedding path (no network to the model host) |

The honest summary: **every line of business logic is tested; almost no line of network I/O has
met the internet.** `make verify-sources` is the tool that closes that gap, and running it is the
first deploy step.

Two smaller caveats:

- `http2 on;` in `deploy/nginx/app-tls.conf` needs nginx ≥ 1.25. Compose pins `nginx:1.27-alpine`,
  so it is correct there; the local 1.24 used for validation rejects it, and everything else in
  that file validated.
- The Haiku model ID is written as `claude-haiku-4-5-20251001` throughout, as requested. The
  undated `claude-haiku-4-5` is also valid and the code normalises dated suffixes before pricing
  lookup and effort gating, so either works.

---

## Start here (five minutes, local)

```bash
cp .env.example .env
docker compose up --build         # http://localhost:8080
```

Sign in with `APP_AUTH_TOKEN` from `.env`. A **DEMO DATA** banner sits on every page: prices are
synthetic and analyses are canned. It disappears by itself once you configure a real provider —
there is no flag to remember to unset.

---

## Deploy to the VPS, in order

**There is a script that does all of this: `deploy/deploy.sh`.** Run it on the server as root,
with `DOMAIN` and `LETSENCRYPT_EMAIL` set. It is idempotent, prints PASS/FAIL per step, exits
non-zero on the first failure, and stops to ask you at four points: for the two API keys (read
hidden, never echoed), after `verify-sources` (only you can judge whether a moved feed is
acceptable), before the rate-limited certificate request, and before rebooting. It will not set
`LLM_FAKE_MODE=false`, will not change `DEPLOYED_AT` once written, will not enable the shadow
model, and opens no port but 22/80/443.

```bash
DOMAIN=market.example.com LETSENCRYPT_EMAIL=you@example.com sudo -E ./deploy/deploy.sh
```

The steps below are what it does, in case you would rather drive it by hand or need to debug a
step it stopped on. Each assumes the previous one passed. Do not skip step 3.

### 1. Server

```bash
ssh root@YOUR_IP
adduser --disabled-password --gecos "" app && usermod -aG sudo app
apt update && apt install -y docker.io docker-compose-v2 git ufw
usermod -aG docker app
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
```

`ufw` is defence in depth only. **Docker writes its own iptables rules in the DOCKER chain, which
is consulted before the FORWARD chain ufw manages** — a published container port is reachable from
the internet whatever `ufw status` says. What actually protects Postgres is that it has no
`ports:` entry. `make compose-check` fails the build if that ever changes.

### 2. Clone and configure

```bash
su - app
git clone https://github.com/jr1733/Trump-trading-.git app && cd app
git checkout claude/trump-market-intelligence-pwa-nctq2d
cp .env.example .env

python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # APP_AUTH_TOKEN
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # POSTGRES_PASSWORD
docker run --rm -v "$PWD/scripts:/s:ro" python:3.11-slim sh -c \
  "pip install -q cryptography && python /s/generate_vapid_keys.py"
```

Edit `.env`:

```bash
ENVIRONMENT=production
APP_AUTH_TOKEN=<generated>
POSTGRES_PASSWORD=<generated>
DOMAIN=yourdomain.com
CORS_ORIGINS=https://yourdomain.com
HTTP_PORT=80
HTTPS_PORT=443
NGINX_CONF=app.conf            # TLS comes in step 4

DEPLOYED_AT=2026-09-17         # TODAY. Never change it afterwards.

MARKET_DATA_PROVIDER=stooq
MARKET_DATA_FALLBACK_PROVIDER=alphavantage
MARKET_DATA_API_KEY=<free key from alphavantage.co>

ENABLED_SOURCES=mock,whitehouse,federal_register,news_rss
LLM_FAKE_MODE=true             # stays true for the first week — see step 6
WEB_PUSH_PUBLIC_KEY=<generated>
WEB_PUSH_PRIVATE_KEY=<generated>
WEB_PUSH_SUBJECT=mailto:you@example.com
```

`DEPLOYED_AT` is the boundary between backfilled history and data this deployment collected
itself. Setting it late, or moving it, destroys the only uncontaminated evaluation the tool can
ever produce.

### 3. Verify the endpoints — before trusting anything

```bash
docker compose build
docker compose run --rm --no-deps api python manage.py verify-sources --all-sources -v
docker compose run --rm --no-deps api python manage.py verify-sources --market-only
```

**Expect failures.** That is what the script is for; it is not a sign the deploy is broken.

- `EMPTY` is the dangerous state, not `FAILED`. It means the URL still serves *something* that is
  no longer a feed — the failure that otherwise hides for weeks. Fix by pointing
  `WHITEHOUSE_FEEDS` / `NEWS_RSS_FEEDS` at the current paths.
- A failing Stooq symbol: mapping is in `backend/app/market/stooq_provider.py` (`_SYMBOL_OVERRIDES`).
  `VIX` and `TNX` are the likely wrong ones; ordinary equities use a mechanical `.us` suffix.
- A failing Alpha Vantage symbol: `backend/app/market/alphavantage_provider.py`. Note `VIX` and
  `TNX` resolve to **proxies** (`VIXY`, `IEF`) there — `IEF` is a bond *price*, not a yield.
- `NEEDS KEY` (Congress) and `MANUAL ONLY` (Truth Social, OGE) are configuration states, not
  failures, and never fail the run.

Iterate until the primary provider and your enabled sources come back `OK`.

### 4. Start, then TLS

```bash
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose logs -f worker      # confirm polling

# First certificate — nginx must be on app.conf (HTTP) for the challenge to work.
docker compose run --rm certbot certonly --webroot -w /var/www/certbot \
  -d yourdomain.com --agree-tos -m you@example.com --no-eff-email

sed -i 's/^NGINX_CONF=.*/NGINX_CONF=app-tls.conf/' .env
docker compose up -d nginx
```

The `certbot` service then renews automatically every 12 hours; `app-tls.conf` keeps the ACME
challenge location ahead of the HTTPS redirect so renewal keeps working.

### 5. Clean out anything synthetic

`manage.py seed` runs on every compose start and loads **reference data only** — tickers, aliases,
entities, the user, watchlist, alert rules, preferences and source rows. No events, no prices.
That is asserted by `test_seed_loads_no_events_and_no_prices`.

But if you ever ran `make demo` or `manage.py import-archive` against this database:

```bash
docker compose exec api python manage.py purge-mock --dry-run
docker compose exec api python manage.py purge-mock
```

It deletes mock/archive events and everything hanging off them (analyses, shadow analyses,
signals, historical matches, embeddings, ticker links, notifications, raw rows) plus mock-provider
price bars. Reference data is untouched. Add `--all-notifications` to clear digests too.

### 6. First week: no model calls

Leave `LLM_FAKE_MODE=true`. You get polling, dedup, ticker matching and the relevance gate under
real volume for zero tokens, and you find the broken feeds before paying per item to process them.

Then:

```bash
sed -i 's/^LLM_FAKE_MODE=.*/LLM_FAKE_MODE=false/' .env
docker compose up -d api worker
```

Everything analysed during that week is **re-analysed properly**. The analysis cache is keyed on
`(content_hash, model, mode)`, so a canned result can never be inherited by the real model. That
was a real bug and there is a regression test for it (`test_analysis_cache.py`).

### 7. iPhone

Open `https://yourdomain.com` in **Safari** → Share → **Add to Home Screen**, then open it from
the icon. Settings → Enable notifications → Send test notification. Push cannot be enabled from a
Safari tab on iOS — this is the single most common reason notifications appear broken.

### 8. Backups

The `backup` service dumps nightly at `BACKUP_HOUR` UTC with 7-day retention, using the same
Postgres image as the database so versions cannot drift. Verify once by hand:

```bash
make backup-now
ls -la backups/
make restore DUMP=backups/trumpmarket-<newest>.dump
```

The dump is written to `.partial` and renamed on success, so an interrupted dump can never be
mistaken for a complete one; retention deletes only after a successful dump, so one bad night
cannot age out the backups that still work. This round trip was executed and verified here (297
events, 25 023 prices, 8 analyses — restored exactly).

---

## Known-sharp edges

- **Backtest numbers on mock data measure the mock data.** Not a finding.
- **The rule-based lexicon carries hindsight.** Written in 2026 knowing which topics moved markets
  over the scored period. It is the *cleaner* backtest mode, not a clean one. Don't let that slip
  in a write-up.
- **Non-overlapping is now the default** on the backtest page. N falls hard (on demo data 87 → 40,
  Sharpe −1.15 → −0.57). The smaller number is the honest one.
- **Forward-only says nothing for months.** That is the design, not a bug.
- **`ALERTS_REQUIRE_USABLE_SAMPLE=true`** is a hard floor under every alert rule. If alerts seem
  quiet, that is why.
- **The default embedder is lexical, not semantic.**
- **Changing scoring logic does not rewrite stored signals** until the pipeline reprocesses those
  events.
- **Alpha Vantage `VIX`/`TNX` are proxies, not the real series.** The provider is recorded on every
  price row, so you can tell which bars came from where.

---

## Next things worth doing

1. **Verify endpoints and fix what is broken** (step 3). Nothing else matters until this passes.
2. **Run a week on real data with `LLM_FAKE_MODE=true`.**
3. **Turn the model on** and watch `/data-quality` daily. The per-source volume and spend table is
   how you find out which source is eating `LLM_DAILY_CALL_BUDGET=150`.
4. **Consider `SHADOW_ANALYSIS_MODEL=claude-opus-5`** at `SHADOW_SAMPLE_RATE=0.1` for a fortnight,
   then read the direction-disagreement figure on `/data-quality`. Low means stay on Haiku. It is
   a second bill, so turn it off again once you have the answer.
5. **Label ~100 events by hand** and measure the relevance gate's precision and recall. Its
   thresholds are guesses that have never been scored.
6. **In ~6 months, run a forward-only, non-overlapping backtest.** That is the first number from
   this tool worth taking seriously.

---

## Where things live

| I want to… | Look at |
|---|---|
| change how a score is built | `backend/app/pipeline/signals.py` |
| change what reaches a model | `backend/app/pipeline/relevance.py`, `runner.py` (`triage_relevance_floor`) |
| add a source | `backend/app/sources/` — subclass `SourceAdapter`, register in `registry.py` |
| change backtest mechanics | `backend/app/pipeline/backtest.py` |
| compare two models | `backend/app/pipeline/shadow.py` |
| fix a feed URL | `.env` — no code change needed |
| fix a market symbol | `backend/app/market/{stooq,alphavantage}_provider.py` |
| change TLS or routing | `deploy/nginx/` |
| see what it is costing | `/data-quality`, or `GET /api/admin/data-quality` |
