"""Application configuration.

Every setting is read from the environment. Secrets never reach the browser:
the only value exposed through the public `/api/config` endpoint is the Web Push
*public* key, which is public by design.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# pydantic-settings JSON-decodes complex types before validators run, which
# would reject a plain comma-separated env var. NoDecode hands us the raw string
# so `_split_list` below can accept both `a,b,c` and `["a","b","c"]`.
StrList = Annotated[list[str], NoDecode]

DEFAULT_NEWS_FEEDS = [
    # Configurable. These are defaults only -- override with NEWS_RSS_FEEDS.
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
]

DEFAULT_WHITEHOUSE_FEEDS = [
    # The adapter tries these in order and uses the first that parses as a feed.
    # Site structure changes between administrations; see docs/PHASE0_FEASIBILITY.md.
    "https://www.whitehouse.gov/presidential-actions/feed/",
    "https://www.whitehouse.gov/briefings-statements/feed/",
    "https://www.whitehouse.gov/feed/",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- core -------------------------------------------------------------
    app_name: str = "Trump Event Market Intelligence"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/trumpmarket"
    app_auth_token: str = "dev-token-change-me"
    cors_origins: StrList = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Anthropic --------------------------------------------------------
    anthropic_api_key: str | None = None
    # Cheap model for relevance triage; stronger model for the full analysis.
    anthropic_triage_model: str = "claude-haiku-4-5"
    anthropic_analysis_model: str = "claude-opus-5"
    anthropic_analysis_effort: str = "medium"
    anthropic_max_analysis_retries: int = 1
    # Hard cap so a stuck source cannot run up a bill.
    llm_daily_call_budget: int = 400
    # When true, use the offline canned scorer instead of calling Anthropic.
    # Results are stamped with the model name "canned-mock" in the UI.
    llm_fake_mode: bool = False

    # --- embeddings (Phase 2) ---------------------------------------------
    # hashing               -- dependency-free, deterministic, LEXICAL not semantic
    # sentence-transformers -- real local model (see requirements-embeddings.txt)
    embedding_provider: str = "hashing"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Both providers emit this dimension, so switching providers needs a
    # re-embed but never a schema migration. 384 = all-MiniLM-L6-v2.
    embedding_dim: int = 384
    embedding_batch_size: int = 32
    # Cosine similarity floor for "similar events". Below this, two events are
    # not treated as comparable however close they rank.
    # Left unset so each provider's own threshold applies; set it to override.
    similarity_threshold: float | None = None
    similar_events_limit: int = 10
    novelty_window_days: int = 30

    # --- event study (Phase 2) --------------------------------------------
    # Market-model estimation window, in trading days before the event.
    event_study_estimation_days: int = 180
    # Gap between the estimation window and the event, so the run-up does not
    # contaminate the alpha/beta estimate.
    event_study_gap_days: int = 5
    event_study_min_observations: int = 60

    # --- market data ------------------------------------------------------
    market_data_provider: str = "mock"  # mock | stooq
    market_data_api_key: str | None = None
    market_context_symbols: StrList = Field(
        default_factory=lambda: ["SPY", "QQQ", "VIX", "TNX"]
    )
    benchmark_symbol: str = "SPY"

    # --- sources ----------------------------------------------------------
    news_api_key: str | None = None
    congress_api_key: str | None = None
    # OGE publishes documents, not an API. Point this at a structured index you
    # are entitled to use, or leave it unset and import filings manually.
    oge_feed_url: str | None = None
    news_rss_feeds: StrList = Field(default_factory=lambda: list(DEFAULT_NEWS_FEEDS))
    whitehouse_feeds: StrList = Field(default_factory=lambda: list(DEFAULT_WHITEHOUSE_FEEDS))
    federal_register_base: str = "https://www.federalregister.gov/api/v1/documents.json"
    enabled_sources: StrList = Field(
        default_factory=lambda: ["mock", "whitehouse", "federal_register", "news_rss"]
    )
    http_timeout_seconds: float = 20.0
    http_user_agent: str = "TrumpEventMarketIntelligence/0.1 (research tool; contact: operator)"

    # --- web push (keys absent => push disabled, in-app still works) -------
    web_push_public_key: str | None = None
    web_push_private_key: str | None = None
    web_push_subject: str | None = None
    web_push_ttl_seconds: int = 3600
    # A push that has failed this many times in a row retires the subscription,
    # so a permanently broken endpoint stops consuming retry budget.
    web_push_max_failures: int = 5
    web_push_max_retries: int = 3

    # --- email (Phase 3; credentials absent => email disabled) -------------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None

    # --- signal weights (shown in the UI, see README "Signal") -------------
    signal_weight_sentiment: float = 0.35
    signal_weight_historical: float = 0.35
    signal_weight_consistency: float = 0.20
    signal_weight_novelty: float = 0.10
    # Normalisation divisor for median abnormal return: a 5% median abnormal
    # move maps to a full-scale component of 1.0.
    signal_return_scale: float = 0.05
    signal_threshold_strong_bull: float = 0.6
    signal_threshold_bull: float = 0.2
    signal_threshold_bear: float = -0.2
    signal_threshold_strong_bear: float = -0.6
    # Sample-size gates from the spec.
    min_reliable_sample: int = 20
    min_usable_sample: int = 10
    # Below `min_usable_sample` the score is sentiment-only and capped here, so
    # a text reading with no comparable history cannot reach a STRONG label
    # (|0.6|). Keep this below signal_threshold_strong_bull or the cap does
    # nothing.
    signal_text_only_cap: float = 0.4
    # Congress moves hundreds of bills a week. Its adapter applies its own
    # keyword gate before anything is stored, stricter than the pipeline's 0.3.
    congress_relevance_threshold: float = 0.5
    # An item scoring below this is not escalated to cheap-model triage. The
    # escalation exists for *borderline* items; paying a model call to confirm
    # that a post-office naming is irrelevant is the cost leak it was meant to
    # prevent. Set to 0.0 to triage everything that fails the rule gate.
    triage_relevance_floor: float = 0.15
    # Unreliable-sample signals never raise a threshold alert. Set false only if
    # you want to be woken by a score computed from text alone.
    alerts_require_usable_sample: bool = True
    # Which horizon drives the signal when several are available.
    signal_primary_horizon: str = "1d"

    # --- digests (Phase 3) ------------------------------------------------
    digest_enabled: bool = True
    # Hourly digests are opt-in; the daily one is the default cadence.
    hourly_digest_enabled: bool = False

    # --- scheduling (minutes) --------------------------------------------
    poll_interval_high_minutes: int = 2
    poll_interval_normal_minutes: int = 5
    poll_interval_slow_minutes: int = 20
    pipeline_interval_minutes: int = 1
    source_failure_alert_threshold: int = 3

    @field_validator(
        "cors_origins",
        "news_rss_feeds",
        "whitehouse_feeds",
        "enabled_sources",
        "market_context_symbols",
        mode="before",
    )
    @classmethod
    def _split_list(cls, value: Any) -> Any:
        """Accept both JSON arrays and comma-separated strings from the env."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def push_enabled(self) -> bool:
        return bool(self.web_push_public_key and self.web_push_private_key)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.email_from)

    def signal_weights(self) -> dict[str, float]:
        return {
            "sentiment": self.signal_weight_sentiment,
            "historical": self.signal_weight_historical,
            "consistency": self.signal_weight_consistency,
            "novelty": self.signal_weight_novelty,
        }

    def signal_thresholds(self) -> dict[str, float]:
        return {
            "strongly_bullish": self.signal_threshold_strong_bull,
            "bullish": self.signal_threshold_bull,
            "bearish": self.signal_threshold_bear,
            "strongly_bearish": self.signal_threshold_strong_bear,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
