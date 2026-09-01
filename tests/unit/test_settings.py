"""Settings, including the two things that silently break a deploy."""

from __future__ import annotations

import pytest

# pydantic's own module, not the transitive pydantic-core distribution — an
# import of the latter would be relying on someone else's dependency tree.
from config.settings import Settings


def _settings(**overrides) -> Settings:
    base = {"DATABASE_URL": "postgresql+asyncpg://localhost:5433/crossover"}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # Heroku hands out postgres:// — the form asyncpg cannot use. Getting
        # this wrong is the classic "works locally, 500s on deploy" bug.
        (
            # Fabricated user:password — the point is the scheme rewrite.
            "postgres://u:p@ec2.compute.amazonaws.com:5432/d",  # pragma: allowlist secret
            "postgresql+asyncpg://u:p@ec2.compute.amazonaws.com:5432/d",  # pragma: allowlist secret
        ),
        (
            "postgresql://u:p@host:5432/d",  # pragma: allowlist secret
            "postgresql+asyncpg://u:p@host:5432/d",  # pragma: allowlist secret
        ),
        # Already correct — must be left alone, not double-prefixed.
        (
            "postgresql+asyncpg://host:5432/d",
            "postgresql+asyncpg://host:5432/d",
        ),
    ],
)
def test_database_url_is_normalized_for_asyncpg(given, expected) -> None:
    assert _settings(DATABASE_URL=given).database_url == expected


def test_the_committed_default_carries_no_credentials() -> None:
    """A committed `user:password@host` DSN is what teaches people to commit
    real ones, and SonarQube flags it as a BLOCKER on a public repo."""
    default = Settings.model_fields["database_url_raw"].default
    assert "@" not in default.split("//", 1)[1].split("/")[0], (
        f"the default DSN embeds credentials: {default}"
    )


def test_marvel_credentials_need_both_halves() -> None:
    """One key alone cannot sign a request, so it counts as unconfigured."""
    assert not _settings().has_marvel_credentials
    assert not _settings(MARVEL_PUBLIC_KEY="pub").has_marvel_credentials
    assert not _settings(MARVEL_PRIVATE_KEY="priv").has_marvel_credentials
    assert _settings(
        MARVEL_PUBLIC_KEY="pub", MARVEL_PRIVATE_KEY="priv"
    ).has_marvel_credentials
