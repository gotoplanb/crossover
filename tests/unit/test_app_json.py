"""The Heroku deploy template must stay in step with the settings.

The failure this prevents: someone adds a required setting, the app still runs
locally because their `.env` has it, and the one-click deploy button produces an
app that crashes on boot for everyone who presses it — with a stack trace about
a missing environment variable and no indication that the template was the
problem.

`app.json` is only ever exercised by a stranger pressing a button, so nothing
else would catch it.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from config.settings import Settings

REPO = Path(__file__).resolve().parent.parent.parent
APP_JSON = json.loads((REPO / "app.json").read_text())


def _aliases() -> dict[str, object]:
    """Setting alias -> field, for every setting the app reads from the env."""
    return {
        (field.alias or name): field
        for name, field in Settings.model_fields.items()
        if field.alias
    }


#: Supplied by Heroku itself or by the addon, so the template must not ask.
PROVIDED_BY_PLATFORM = {"DATABASE_URL", "PORT"}

#: Deliberately not offered by the deploy template. Marvel's API is
#: discontinued (docs/gates.md), so prompting for keys that can no longer be
#: obtained — and that would do nothing if they were — is worse than silence.
DELIBERATELY_OMITTED = {"MARVEL_PUBLIC_KEY", "MARVEL_PRIVATE_KEY"}


#: Read at call time rather than declared as Settings fields, so they cannot be
#: discovered from the model. Reader passwords especially: the set of readers is
#: *data*, and adding one should be a config var and a seed, not a code change.
DYNAMIC_ENV_VARS = {
    "CROSSOVER_OWNER_EMAIL",     # consumed by `crossover bootstrap`
    "CROSSOVER_OWNER_HANDLE",    # ditto
    "CROSSOVER_PASSWORD_OWNER",  # Settings.reader_password("owner")
}


def test_every_declared_env_var_is_a_real_setting() -> None:
    """A typo in app.json sets a variable nothing reads, which looks like it
    worked and silently doesn't."""
    known = set(_aliases()) | DYNAMIC_ENV_VARS
    unknown = set(APP_JSON["env"]) - known
    assert not unknown, f"app.json declares settings nothing reads: {sorted(unknown)}"


def test_the_dynamic_vars_are_genuinely_read() -> None:
    """The allowlist above is an escape hatch, so it has to be checked against
    the code rather than trusted — otherwise it becomes a place typos hide."""
    cli = (REPO / "scripts" / "cli.py").read_text()
    assert "CROSSOVER_OWNER_EMAIL" in cli
    assert "CROSSOVER_OWNER_HANDLE" in cli

    from config.settings import Settings

    assert "CROSSOVER_PASSWORD_" in inspect.getsource(Settings.reader_password)


def test_the_first_reader_gets_a_generated_password() -> None:
    """Otherwise a button deploy creates an admin who cannot sign in."""
    entry = APP_JSON["env"]["CROSSOVER_PASSWORD_OWNER"]
    assert entry.get("generator") == "secret"
    assert "value" not in entry


def test_every_setting_without_a_default_is_offered() -> None:
    """A required setting missing from the template means the button produces an
    app that cannot boot."""
    required = {
        alias
        for alias, field in _aliases().items()
        if field.is_required() and alias not in PROVIDED_BY_PLATFORM
    }
    missing = required - set(APP_JSON["env"])
    assert not missing, (
        f"these have no default and are not in app.json, so a one-click deploy "
        f"would fail to start: {sorted(missing)}"
    )


def test_there_is_no_master_admin_key() -> None:
    """Admin is a per-reader flag, so each admin has their own credential.

    A shared master key would undo that: anyone holding it would be an admin
    regardless of which reader they signed in as, and revoking a session would
    not revoke curation access.
    """
    assert "CROSSOVER_ADMIN_KEY" not in APP_JSON["env"]
    assert "CROSSOVER_ADMIN_KEY" not in json.dumps(APP_JSON)


def test_no_secret_is_hardcoded_in_the_template() -> None:
    from scripts.check_secrets import scan_text

    assert scan_text((REPO / "app.json").read_text(), "app.json") == []


def test_marvel_keys_are_not_prompted_for() -> None:
    """They can no longer be obtained and would do nothing. Asking would send a
    deployer to a registration page that 301s away."""
    assert not (set(APP_JSON["env"]) & DELIBERATELY_OMITTED)


def test_the_owner_email_is_required_so_the_app_is_usable() -> None:
    """Without a reader the allowlist is empty, and the login page has nothing to
    sign in as — which reads as broken rather than unconfigured."""
    assert APP_JSON["env"]["CROSSOVER_OWNER_EMAIL"]["required"] is True
    assert APP_JSON["scripts"]["postdeploy"].endswith("bootstrap")


def test_the_public_url_is_required_and_flagged_as_correctable() -> None:
    """It cannot be known before the app is named, so the template has to make
    the placeholder obviously wrong rather than plausibly right."""
    entry = APP_JSON["env"]["CROSSOVER_PUBLIC_URL"]
    assert entry["required"] is True
    assert "CHANGE-ME" in entry["value"]
    assert "after deploy" in entry["description"]


def test_https_is_forced_for_the_session_cookie() -> None:
    assert APP_JSON["env"]["UI_COOKIE_SECURE"]["value"] == "true"


def test_telemetry_is_off_by_default() -> None:
    """The collector it targets is a local Alloy, which a dyno cannot reach; on
    means every request retries an export that can never succeed."""
    assert APP_JSON["env"]["OTEL_ENABLED"]["value"] == "false"


def test_a_database_is_attached() -> None:
    plans = [a["plan"] for a in APP_JSON["addons"]]
    assert any(p.startswith("heroku-postgresql") for p in plans), plans


@pytest.mark.parametrize("field", ["name", "description", "repository", "success_url"])
def test_the_template_is_described(field: str) -> None:
    assert APP_JSON.get(field)


def test_every_env_entry_explains_itself() -> None:
    """These strings are the entire UI of a one-click deploy."""
    for name, entry in APP_JSON["env"].items():
        assert entry.get("description"), f"{name} has no description"
        assert len(entry["description"]) > 40, f"{name}'s description is too thin"


def test_the_release_phase_migrates() -> None:
    """`postdeploy` runs only for a deploy-button install; `release` runs on
    every deploy, so migrations belong there."""
    procfile = (REPO / "Procfile").read_text()
    assert "release: alembic upgrade head" in procfile
