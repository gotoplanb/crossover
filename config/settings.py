import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from models.user import valid_handle

#: The repo root, so the .env fallback below resolves the same regardless of
#: which directory a command was run from.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    # Absolute path, not the bare ".env" pydantic defaults to. That form is
    # resolved against the *current working directory*, so `python -m
    # scripts.cli ...` from anywhere but the repo root silently failed to find
    # the file, and every setting silently fell back to its default. Invisible
    # on Heroku, where config vars are real environment variables, and
    # confusing everywhere else.
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

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

    # Set true when served over HTTPS so the admin cookie carries Secure.
    ui_cookie_secure: bool = Field(default=False, alias="UI_COOKIE_SECURE")

    # Shared secret required to create an account. **Unset closes registration
    # entirely** rather than opening it: this app writes to a database and
    # spends a rate-limited third-party quota on every shelf lookup, so an
    # accidentally open form is a way for a stranger to spend both. Failing
    # closed means forgetting to configure it cannot be the mistake that opens
    # the door.
    invite_code: str | None = Field(default=None, alias="CROSSOVER_INVITE_CODE")

    # How long a cached Marvel response is considered fresh. The cache is
    # disposable (SPEC §3) so this is a politeness knob, not correctness.
    cache_ttl_hours: int = Field(default=24 * 7, alias="MARVEL_CACHE_TTL_HOURS")

    # --- OpenTelemetry -> Watchtower's Alloy collector ---
    # Alloy receives OTLP on 4317 (gRPC) and fans traces out to Tempo and logs
    # to Loki. From inside a container, use host.docker.internal:4317.
    otel_endpoint: str = Field(default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
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
    def registration_open(self) -> bool:
        return bool(self.invite_code)

    @property
    def has_marvel_credentials(self) -> bool:
        return bool(self.marvel_public_key and self.marvel_private_key)

    @staticmethod
    def _env_file_values() -> dict[str, str]:
        """Parse the .env file the same way pydantic-settings does.

        Needed because reader passwords are looked up by *dynamic* key —
        `CROSSOVER_PASSWORD_{HANDLE}` — so they cannot be declared as fields and
        pydantic never loads them. Without this, a password in .env would work
        in production (where config vars are real environment variables) and
        silently fail locally, which is the worst possible split.
        """
        if not ENV_FILE.exists():
            return {}
        values: dict[str, str] = {}
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        return values

    def reader_password(self, handle: str) -> str | None:
        """The password for one reader, from `CROSSOVER_PASSWORD_{HANDLE}`.

        Read from the environment at call time rather than declared as fields,
        because the set of readers is data — adding one should be a config var
        and a seed, not a code change.

        Stored as the plaintext the operator chose. For a two-person deployment
        that is the same exposure any config var carries: visible to anyone who
        can read the config, and not a hash. If this ever grows past people who
        share a household, hash them.
        """
        if not valid_handle(handle):
            return None
        key = f"CROSSOVER_PASSWORD_{handle.upper()}"
        # Real environment first, so a config var always beats a stale .env.
        return os.environ.get(key) or self._env_file_values().get(key) or None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
