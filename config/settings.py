from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Values that must never guard a real deployment. This repo is public, so a
#: defaulted or example admin key is a *published* credential — anyone reading
#: the source would hold the key to the curation views and OAuth consent.
#: `admin_key` therefore has no default at all (missing it fails at startup);
#: this list catches the next-worst case, where someone copies .env.example
#: verbatim and ships it.
WEAK_ADMIN_KEYS = frozenset(
    {"change-me", "changeme", "dev-admin-key", "admin", "password", "secret", "test"}
)

#: Shorter than this is not worth calling a key.
MIN_ADMIN_KEY_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Heroku hands us a postgres:// URL; SQLAlchemy's asyncpg driver needs
    # postgresql+asyncpg://. Normalized in `database_url` below rather than
    # asking every deploy to remember.
    #
    # The default carries **no credentials on purpose**. A committed
    # `user:password@host` DSN is the pattern that teaches people to commit real
    # ones, and SonarQube rightly flags it as a BLOCKER on a public repo. The
    # working local DSN lives in .env (gitignored); .env.example has the shape.
    database_url_raw: str = Field(
        default="postgresql+asyncpg://localhost:5433/crossover",
        alias="DATABASE_URL",
    )

    # Marvel developer credentials (https://developer.marvel.com). Without both,
    # the API client refuses to make a call rather than sending an unsigned one.
    marvel_public_key: str | None = Field(default=None, alias="MARVEL_PUBLIC_KEY")
    marvel_private_key: str | None = Field(default=None, alias="MARVEL_PRIVATE_KEY")

    # Public HTTPS origin. The OAuth issuer, and the base for the authorize /
    # token URLs that go into a Claude custom connector.
    public_base_url: str = Field(default="http://localhost:8000", alias="CROSSOVER_PUBLIC_URL")

    # Admin session key for the curation views + OAuth consent approval.
    # **No default on purpose.** A default in a public repo is a published
    # credential; the app failing to boot is the correct outcome of forgetting
    # to set this. Generate one with `make admin-key`.
    admin_key: str = Field(alias="CROSSOVER_ADMIN_KEY")

    # Set true when served over HTTPS so the admin cookie carries Secure.
    ui_cookie_secure: bool = Field(default=False, alias="UI_COOKIE_SECURE")

    # How long a cached Marvel response is considered fresh. The cache is
    # disposable (SPEC §3) so this is a politeness knob, not correctness.
    cache_ttl_hours: int = Field(default=24 * 7, alias="MARVEL_CACHE_TTL_HOURS")

    # --- OpenTelemetry -> Watchtower's Alloy collector ---
    # Alloy receives OTLP on 4317 (gRPC) and fans traces out to Tempo and logs
    # to Loki. From inside a container, use host.docker.internal:4317.
    otel_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_service_name: str = Field(default="crossover", alias="OTEL_SERVICE_NAME")
    # Off in tests: a BatchSpanProcessor with nothing listening retries in the
    # background and makes the suite slow and noisy for no signal.
    otel_enabled: bool = Field(default=True, alias="OTEL_ENABLED")

    @property
    def database_url(self) -> str:
        url = self.database_url_raw
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def has_marvel_credentials(self) -> bool:
        return bool(self.marvel_public_key and self.marvel_private_key)

    @property
    def admin_key_is_weak(self) -> bool:
        """True if the admin key is an example value or too short to matter.

        Not a hard failure: blocking boot would make local development
        needlessly painful, and the key being weak is only dangerous once the
        thing is reachable. The lifespan logs it loudly and `/healthz` reports
        it, so it cannot be missed on a real deploy.
        """
        return (
            self.admin_key.lower() in WEAK_ADMIN_KEYS
            or len(self.admin_key) < MIN_ADMIN_KEY_LENGTH
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
