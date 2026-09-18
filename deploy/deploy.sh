#!/usr/bin/env bash
# Production deploy for the Event -> Market Intelligence PWA.
#
#   sudo ./deploy/deploy.sh
#
# Run it ON THE VPS, from a checkout of
# claude/trump-market-intelligence-pwa-nctq2d. It is idempotent: re-running it
# after fixing something picks up where it left off rather than starting over.
#
# It STOPS and asks you at four points, and those stops are the point of it:
#
#   * for ANTHROPIC_API_KEY and MARKET_DATA_API_KEY (never echoed, never logged);
#   * after `verify-sources`, because a moved feed is the failure that otherwise
#     hides for weeks and only you can say whether the result is acceptable;
#   * before the certificate request, which is rate-limited by Let's Encrypt;
#   * before rebooting.
#
# Things it will not do, per the deploy brief:
#   * set LLM_FAKE_MODE=false             (week one runs offline, deliberately)
#   * change DEPLOYED_AT once it is set   (it is the forward-only boundary)
#   * enable the shadow model             (it is a second bill)
#   * open any port but 22, 80 and 443
#
# Every step prints PASS or FAIL. The script exits on the first failure with a
# non-zero status, so `echo $?` after it is meaningful.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Fill these in, or export them before running.
# ---------------------------------------------------------------------------
DOMAIN="${DOMAIN:-<DOMAIN>}"                 # e.g. market.example.com
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-<EMAIL>}"
REPO_URL="${REPO_URL:-https://github.com/jr1733/Trump-trading-.git}"
BRANCH="${BRANCH:-claude/trump-market-intelligence-pwa-nctq2d}"
APP_DIR="${APP_DIR:-/home/app/app}"
APP_USER="${APP_USER:-app}"

# ---------------------------------------------------------------------------
readonly RED=$'\033[31m' GREEN=$'\033[32m' YELLOW=$'\033[33m' DIM=$'\033[2m' RESET=$'\033[0m'
STEP=0

step()  { STEP=$((STEP + 1)); printf '\n%s=== STEP %d: %s ===%s\n' "$DIM" "$STEP" "$1" "$RESET"; }
pass()  { printf '  %sPASS%s  %s\n' "$GREEN" "$RESET" "$1"; }
warn()  { printf '  %sWARN%s  %s\n' "$YELLOW" "$RESET" "$1"; }
fail()  { printf '  %sFAIL%s  %s\n' "$RED" "$RESET" "$1"; exit 1; }
note()  { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }

confirm() {
    local prompt="$1" reply
    printf '\n%s%s%s [y/N] ' "$YELLOW" "$prompt" "$RESET"
    read -r reply </dev/tty
    [[ "$reply" =~ ^[Yy]$ ]]
}

trap 'printf "\n%sdeploy aborted at step %d%s\n" "$RED" "$STEP" "$RESET"' ERR

# `docker compose` needs to run as the app user, in the app dir.
dc() { sudo -u "$APP_USER" -H env -C "$APP_DIR" docker compose "$@"; }

# ---------------------------------------------------------------------------
step "Preflight"
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || fail "run as root (sudo ./deploy/deploy.sh)"

if [[ "$DOMAIN" == "<DOMAIN>" || "$LETSENCRYPT_EMAIL" == "<EMAIL>" ]]; then
    fail "DOMAIN and LETSENCRYPT_EMAIL are still placeholders. Edit the top of this
        file, or run:  DOMAIN=market.example.com LETSENCRYPT_EMAIL=you@example.com sudo -E $0"
fi
pass "domain $DOMAIN, contact $LETSENCRYPT_EMAIL"

# DNS must already resolve here, or certbot's HTTP-01 challenge cannot succeed.
resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}')" || true
public_ip="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo unknown)"
if [[ -z "${resolved:-}" ]]; then
    fail "$DOMAIN does not resolve. Point its A record at this server first."
elif [[ "$resolved" != "$public_ip" && "$public_ip" != "unknown" ]]; then
    warn "$DOMAIN resolves to $resolved but this host looks like $public_ip."
    note "If that is a proxy (Cloudflare etc.) the HTTP-01 challenge may fail."
    confirm "Continue anyway?" || fail "stopped at DNS check"
else
    pass "$DOMAIN resolves to $resolved"
fi

# ---------------------------------------------------------------------------
step "Packages and firewall"
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
missing=()
for pkg in docker.io docker-compose-v2 make git ufw curl; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if ((${#missing[@]})); then
    note "installing: ${missing[*]}"
    apt-get update -qq
    apt-get install -y -qq "${missing[@]}"
fi
command -v docker >/dev/null || fail "docker not on PATH after install"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin missing"
systemctl enable --now docker >/dev/null 2>&1 || true
docker info >/dev/null 2>&1 || fail "docker daemon is not running"
pass "docker $(docker --version | awk '{print $3}' | tr -d ,), compose plugin present"

id -u "$APP_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$APP_USER"
usermod -aG docker "$APP_USER"
pass "user $APP_USER exists and is in the docker group"

# Only 22/80/443. Note that ufw does NOT constrain Docker-published ports --
# Docker writes its own rules in the DOCKER chain, consulted before the FORWARD
# chain ufw manages. What actually keeps Postgres private is that it has no
# `ports:` entry; step 4 checks that.
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp  >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
opened="$(ufw status | awk '/ALLOW/ {print $1}' | sort -u | tr '\n' ' ')"
pass "ufw enabled; allowed: $opened"
note "ufw cannot close a Docker-published port. compose-check is the real control."

# ---------------------------------------------------------------------------
step "Checkout"
# ---------------------------------------------------------------------------
if [[ -d "$APP_DIR/.git" ]]; then
    sudo -u "$APP_USER" -H git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    sudo -u "$APP_USER" -H git -C "$APP_DIR" checkout --quiet "$BRANCH"
    sudo -u "$APP_USER" -H git -C "$APP_DIR" merge --ff-only --quiet "origin/$BRANCH"
else
    sudo -u "$APP_USER" -H git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
head_sha="$(sudo -u "$APP_USER" -H git -C "$APP_DIR" rev-parse --short HEAD)"
pass "$BRANCH at $head_sha"

# ---------------------------------------------------------------------------
step "Configuration (.env)"
# ---------------------------------------------------------------------------
ENV_FILE="$APP_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    pass ".env already exists -- leaving secrets and DEPLOYED_AT alone"
    note "delete it by hand if you want a clean regeneration"
else
    sudo -u "$APP_USER" -H cp "$APP_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown "$APP_USER:$APP_USER" "$ENV_FILE"

    # Generated locally; never printed.
    auth_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    pg_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

    # VAPID keys, generated in a throwaway container so the host needs no python deps.
    note "generating VAPID keypair..."
    # generate_vapid_keys.py imports `cryptography` directly -- not py-vapid.
    vapid="$(docker run --rm -v "$APP_DIR/scripts:/s:ro" python:3.11-slim \
        sh -c 'pip install -q cryptography >/dev/null 2>&1 && python /s/generate_vapid_keys.py' 2>/dev/null)"
    vapid_public="$(printf '%s' "$vapid"  | grep -iE '^WEB_PUSH_PUBLIC_KEY='  | cut -d= -f2-)"
    vapid_private="$(printf '%s' "$vapid" | grep -iE '^WEB_PUSH_PRIVATE_KEY=' | cut -d= -f2-)"
    [[ -n "$vapid_public" && -n "$vapid_private" ]] \
        || fail "VAPID generation produced no keys; run scripts/generate_vapid_keys.py by hand"

    printf '\n  %sPaste your ANTHROPIC_API_KEY (input hidden; Enter to leave blank):%s ' "$YELLOW" "$RESET"
    read -rs anthropic_key </dev/tty; echo
    printf '  %sPaste your MARKET_DATA_API_KEY for Alpha Vantage (hidden; Enter to skip):%s ' "$YELLOW" "$RESET"
    read -rs market_key </dev/tty; echo

    # `set` writes a key=value, quoting nothing and echoing nothing.
    set_env() {
        local key="$1" value="$2"
        if grep -qE "^${key}=" "$ENV_FILE"; then
            python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import sys, pathlib
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
out = []
for line in p.read_text().splitlines():
    out.append(f"{key}={value}" if line.startswith(f"{key}=") else line)
p.write_text("\n".join(out) + "\n")
PY
        else
            printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
        fi
    }

    set_env ENVIRONMENT production
    set_env APP_AUTH_TOKEN "$auth_token"
    set_env POSTGRES_PASSWORD "$pg_password"
    set_env DOMAIN "$DOMAIN"
    set_env CORS_ORIGINS "https://$DOMAIN"
    set_env HTTP_PORT 80
    set_env HTTPS_PORT 443
    set_env NGINX_CONF app.conf            # TLS is switched on later, in step 7
    set_env DEPLOYED_AT "$(date -u +%F)"
    set_env LLM_FAKE_MODE true             # week one runs offline, deliberately
    set_env MARKET_DATA_PROVIDER stooq
    set_env MARKET_DATA_FALLBACK_PROVIDER alphavantage
    set_env ENABLED_SOURCES mock,whitehouse,federal_register,news_rss
    set_env WEB_PUSH_PUBLIC_KEY "$vapid_public"
    set_env WEB_PUSH_PRIVATE_KEY "$vapid_private"
    set_env WEB_PUSH_SUBJECT "mailto:$LETSENCRYPT_EMAIL"
    set_env SHADOW_ANALYSIS_MODEL ""       # stays off
    [[ -n "$anthropic_key" ]] && set_env ANTHROPIC_API_KEY "$anthropic_key"
    [[ -n "$market_key"    ]] && set_env MARKET_DATA_API_KEY "$market_key"
    unset anthropic_key market_key auth_token pg_password vapid vapid_private

    chmod 600 "$ENV_FILE"
    pass ".env written (0600, owned by $APP_USER)"
fi

grep -qE '^\.env$' "$APP_DIR/.gitignore" && pass ".env is gitignored" || fail ".env is NOT gitignored"
sudo -u "$APP_USER" -H git -C "$APP_DIR" status --porcelain | grep -q '^.. \.env$' \
    && fail ".env is tracked by git -- stop and remove it from the index" \
    || pass ".env is not staged"

deployed_at="$(grep -E '^DEPLOYED_AT=' "$ENV_FILE" | cut -d= -f2-)"
[[ -n "$deployed_at" ]] && pass "DEPLOYED_AT=$deployed_at (never change this)" \
                        || fail "DEPLOYED_AT is empty"
grep -qE '^LLM_FAKE_MODE=true$' "$ENV_FILE" && pass "LLM_FAKE_MODE=true" \
                                            || fail "LLM_FAKE_MODE must be true for week one"

# ---------------------------------------------------------------------------
step "Network exposure (compose-check)"
# ---------------------------------------------------------------------------
sudo -u "$APP_USER" -H env -C "$APP_DIR" python3 scripts/check_compose_exposure.py \
    || fail "something other than nginx publishes a port"
pass "nginx is the only published service"

# ---------------------------------------------------------------------------
step "Build and verify sources"
# ---------------------------------------------------------------------------
dc build --quiet || fail "image build failed"
pass "images built"

note "probing live feeds and market data -- expect some failures, that is the point"
set +e
dc run --rm --no-deps api python manage.py verify-sources --all-sources -v
verify_status=$?
set -e

printf '\n'
if ((verify_status == 0)); then
    pass "every configured source and the primary market provider responded"
else
    warn "verify-sources exited $verify_status -- read the report above."
    cat <<EOF

  ${DIM}How to read it:
    EMPTY   is MORE dangerous than FAILED. The URL still serves something that
            is no longer a feed. Fix: point WHITEHOUSE_FEEDS / NEWS_RSS_FEEDS
            in .env at the current paths.
    FAILED  on a source     -> wrong URL, or the host blocked the request.
    FAILED  on a symbol     -> wrong mapping. Stooq: backend/app/market/
            stooq_provider.py (_SYMBOL_OVERRIDES). Alpha Vantage: note that
            VIX and TNX resolve to PROXIES (VIXY, IEF) -- IEF is a bond price,
            not a yield.
    FAILED  "rate limited"  -> Alpha Vantage free tier. Wait and re-run.
    NEEDS KEY / MANUAL ONLY -> configuration, not breakage. Never fatal.${RESET}

EOF
fi
confirm "Are these verify-sources results acceptable? Continue?" \
    || fail "stopped so you can fix feeds/symbols in .env, then re-run this script"

# ---------------------------------------------------------------------------
step "Start the stack (HTTP)"
# ---------------------------------------------------------------------------
dc up -d || fail "compose up failed"
for _ in $(seq 1 30); do
    dc exec -T api python -c "
import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" \
        >/dev/null 2>&1 && break
    sleep 2
done
dc exec -T api python -c "
import urllib.request,sys; sys.exit(0 if urllib.request.urlopen(
    'http://localhost:8000/api/health').status==200 else 1)" \
    >/dev/null 2>&1 || fail "api health check never passed -- see: docker compose logs api"
pass "api healthy"

curl -fsS --max-time 10 -o /dev/null "http://$DOMAIN/" \
    && pass "http://$DOMAIN/ serves the app" \
    || fail "http://$DOMAIN/ is not reachable -- check DNS and ufw"

# ---------------------------------------------------------------------------
step "Seed check: reference data only"
# ---------------------------------------------------------------------------
seed_report="$(dc exec -T api python -c "
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models import Event, MarketPrice, RawEvent, Signal, Ticker, EntityAlias, User, AlertRule
db = SessionLocal()
n = lambda m: int(db.execute(select(func.count()).select_from(m)).scalar() or 0)
print(f'events={n(Event)} raw={n(RawEvent)} prices={n(MarketPrice)} signals={n(Signal)}')
print(f'tickers={n(Ticker)} aliases={n(EntityAlias)} users={n(User)} rules={n(AlertRule)}')
")" || fail "could not read the database"
echo "$seed_report" | sed 's/^/  /'

echo "$seed_report" | grep -qE 'events=0 raw=0 prices=0 signals=0' \
    && pass "zero events, zero prices -- seed loaded reference data only" \
    || {
        warn "the database is not empty of events or prices"
        note "if this box ever ran 'make demo', purge with:"
        note "  docker compose run --rm api python manage.py purge-mock --dry-run"
        confirm "Continue anyway?" || fail "stopped at seed check"
    }
echo "$seed_report" | grep -qE 'tickers=[1-9]' \
    && pass "reference data present" || fail "reference data missing -- seed did not run"

# ---------------------------------------------------------------------------
step "TLS certificate"
# ---------------------------------------------------------------------------
if dc run --rm --entrypoint sh certbot -c "test -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem" 2>/dev/null; then
    pass "certificate for $DOMAIN already exists -- not re-requesting"
else
    note "Let's Encrypt rate-limits certificate requests (5 failures/hour/domain)."
    confirm "Request a certificate for $DOMAIN now?" || fail "stopped before certbot"
    dc run --rm certbot certonly --webroot -w /var/www/certbot \
        -d "$DOMAIN" --agree-tos -m "$LETSENCRYPT_EMAIL" --no-eff-email --non-interactive \
        || fail "certbot failed -- the HTTP-01 challenge needs http://$DOMAIN/.well-known/ reachable"
    pass "certificate issued"
fi

python3 - "$ENV_FILE" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_text("\n".join(
    "NGINX_CONF=app-tls.conf" if l.startswith("NGINX_CONF=") else l
    for l in p.read_text().splitlines()) + "\n")
PY
chown "$APP_USER:$APP_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"
dc up -d nginx || fail "nginx would not start on app-tls.conf"
sleep 3

dc exec -T nginx nginx -t 2>&1 | sed 's/^/  /' | tail -2
dc exec -T nginx nginx -t >/dev/null 2>&1 \
    && pass "nginx -t passes on the 1.27 image (http2 on included)" \
    || fail "nginx -t failed -- see output above"

curl -fsS --max-time 15 -o /dev/null "https://$DOMAIN/" \
    && pass "https://$DOMAIN/ loads with a valid certificate" \
    || fail "https://$DOMAIN/ failed -- curl validates the chain, so this is a real TLS problem"

redirect="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "http://$DOMAIN/")"
[[ "$redirect" == 30*"https://$DOMAIN/"* ]] \
    && pass "http -> https redirect: $redirect" \
    || fail "http did not redirect to https (got: $redirect)"

expiry="$(dc run --rm --entrypoint sh certbot -c \
    "openssl x509 -enddate -noout -in /etc/letsencrypt/live/$DOMAIN/fullchain.pem" 2>/dev/null || true)"
[[ -n "$expiry" ]] && note "certificate ${expiry//notAfter=/expires }"

# ---------------------------------------------------------------------------
step "Backup and restore"
# ---------------------------------------------------------------------------
before="$(dc exec -T db psql -U postgres -d trumpmarket -tAc \
    'select (select count(*) from tickers), (select count(*) from users), (select count(*) from events);')"
note "row counts before: tickers|users|events = $before"

dc run --rm backup /usr/local/bin/backup.sh once || fail "backup failed"
newest="$(sudo -u "$APP_USER" -H ls -t "$APP_DIR/backups"/*.dump 2>/dev/null | head -1)"
[[ -n "$newest" ]] && pass "backup written: $(basename "$newest") ($(du -h "$newest" | cut -f1))" \
                   || fail "no dump file appeared in $APP_DIR/backups"

if confirm "Test the restore? This DROPS and recreates the database from that dump."; then
    dc stop api worker >/dev/null
    dc exec -T db dropdb -U postgres --if-exists trumpmarket
    dc exec -T db createdb -U postgres trumpmarket
    dc exec -T db pg_restore -U postgres -d trumpmarket --no-owner <"$newest" \
        || fail "pg_restore failed -- the database is empty, restore by hand before continuing"
    dc start api worker >/dev/null
    sleep 5
    after="$(dc exec -T db psql -U postgres -d trumpmarket -tAc \
        'select (select count(*) from tickers), (select count(*) from users), (select count(*) from events);')"
    note "row counts after:  tickers|users|events = $after"
    [[ "$before" == "$after" ]] && pass "restore verified: counts match exactly" \
                               || fail "counts differ before/after -- investigate before trusting backups"
else
    warn "restore NOT tested -- an untested backup is not a backup"
fi

# The backup container runs its own daily loop (BACKUP_HOUR, UTC) and is
# restart: unless-stopped, so it needs no host cron.
dc ps backup --format '{{.Service}} {{.Status}}' | sed 's/^/  /'
pass "backup service scheduled in-container (BACKUP_HOUR UTC, BACKUP_RETENTION_DAYS days)"

# ---------------------------------------------------------------------------
step "Restart on reboot"
# ---------------------------------------------------------------------------
systemctl is-enabled docker >/dev/null 2>&1 && pass "docker starts at boot" \
                                            || fail "docker is not enabled at boot"
bad="$(dc ps --format '{{.Service}} {{.Labels}}' \
    | grep -v 'restart-policy=unless-stopped\|restart-policy=always' \
    | awk '{print $1}' | grep -v '^migrate$' || true)"
[[ -z "$bad" ]] && pass "every long-running service has restart: unless-stopped" \
               || warn "no restart policy on: $bad"

if confirm "Reboot now and re-verify?"; then
    note "rebooting; re-run this script afterwards to re-verify, or check by hand:"
    note "  curl -sS -o /dev/null -w '%{http_code}\\n' https://$DOMAIN/"
    ( sleep 3; reboot ) &
    exit 0
fi
warn "not rebooted -- restart-on-boot is configured but unproven until you do"

# ---------------------------------------------------------------------------
printf '\n%s=== DONE ===%s\n' "$GREEN" "$RESET"
cat <<EOF

  App:        https://$DOMAIN/
  Sign in:    the APP_AUTH_TOKEN in $ENV_FILE  (cat it over ssh; it is 0600)
  Deployed:   $deployed_at   ${DIM}(the forward-only boundary; never change it)${RESET}
  Model:      ${YELLOW}LLM_FAKE_MODE=true${RESET} -- canned analyses, zero API cost, for week one.

  On your iPhone, in this order -- push will not work otherwise:
    1. Open https://$DOMAIN/ in ${YELLOW}Safari${RESET} (not Chrome).
    2. Share -> Add to Home Screen.
    3. Open the app ${YELLOW}from the Home Screen icon${RESET}, not the Safari tab.
    4. Settings -> Enable notifications -> Send test notification.

  Next week, after the feeds have proven themselves:
    sed -i 's/^LLM_FAKE_MODE=.*/LLM_FAKE_MODE=false/' $ENV_FILE
    docker compose up -d api worker
  Everything analysed this week is re-analysed properly -- the cache is keyed
  on (content, model, mode), so canned results are never inherited.

EOF
