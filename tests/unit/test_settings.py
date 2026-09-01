"""Settings, including the two things that silently break a deploy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticUndefined

from config.settings import MIN_ADMIN_KEY_LENGTH, WEAK_ADMIN_KEYS, Settings


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+asyncpg://localhost:5433/crossover",
        "CROSSOVER_ADMIN_KEY": "a-perfectly-fine-admin-key",
    }
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


def test_the_admin_key_is_required_with_no_default() -> None:
    """Failing to boot is the correct outcome of forgetting to set it — a
    default in a public repo would be a published credential.

    Asserted against the field definition rather than by constructing Settings
    without it: pydantic-settings reads the ambient environment and .env, so a
    construction test would pass for the wrong reason on any machine that
    happens to have the variable set.
    """
    field = Settings.model_fields["admin_key"]
    assert field.is_required(), "admin_key must have no default"
    assert field.default is PydanticUndefined


def test_settings_construction_fails_without_the_admin_key(monkeypatch) -> None:
    """The end result: a process with no admin key configured cannot start."""
    monkeypatch.delenv("CROSSOVER_ADMIN_KEY", raising=False)
    # `.env` would otherwise supply it on a developer machine; point the loader
    # at a file that does not exist so only the environment is consulted.
    with pytest.raises(ValidationError, match="admin_key|CROSSOVER_ADMIN_KEY"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("weak", sorted(WEAK_ADMIN_KEYS))
def test_example_admin_keys_are_reported_as_weak(weak) -> None:
    assert _settings(CROSSOVER_ADMIN_KEY=weak).admin_key_is_weak is True  # pragma: allowlist secret


def test_a_short_admin_key_is_weak() -> None:
    assert _settings(CROSSOVER_ADMIN_KEY="x" * (MIN_ADMIN_KEY_LENGTH - 1)).admin_key_is_weak
    assert not _settings(CROSSOVER_ADMIN_KEY="x" * MIN_ADMIN_KEY_LENGTH).admin_key_is_weak


def test_marvel_credentials_need_both_halves() -> None:
    """One key alone cannot sign a request, so it counts as unconfigured."""
    assert not _settings().has_marvel_credentials
    assert not _settings(MARVEL_PUBLIC_KEY="pub").has_marvel_credentials
    assert not _settings(MARVEL_PRIVATE_KEY="priv").has_marvel_credentials
    assert _settings(
        MARVEL_PUBLIC_KEY="pub", MARVEL_PRIVATE_KEY="priv"
    ).has_marvel_credentials
