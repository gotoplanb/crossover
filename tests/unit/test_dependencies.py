"""Every third-party module we import must be a declared dependency.

The failure this prevents is not hypothetical: `main.py` imports OpenTelemetry
at module level — it has to, because Starlette freezes the middleware stack
before the lifespan runs — and those packages were installed into the local venv
but never added to `pyproject.toml`. Everything passed locally. A fresh install
would have crashed on boot with `ModuleNotFoundError`, which on Heroku means a
failed release with no obvious cause.

A lockfile alone would not have caught it, because the lock is generated *from*
the same incomplete declaration.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent

#: Import name -> distribution name, where they differ.
DISTRIBUTION_NAMES = {
    "yaml": "pyyaml",
    "multipart": "python-multipart",
    "dotenv": "python-dotenv",
    "sqlalchemy": "sqlalchemy",
    "jose": "python-jose",
}

#: Modules that are ours, or the standard library, or pulled in transitively by
#: a declared package in a way that is part of that package's public surface.
LOCAL_PACKAGES = {
    "alembic", "auth", "config", "csrf", "curation", "db", "lifespan", "main",
    "marvel", "mcp_server", "models", "oauth_provider", "observability",
    "routes", "scripts", "service", "templates_env", "tests",
}


def _declared() -> set[str]:
    with (REPO / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    names = set()
    groups = [data["project"]["dependencies"]]
    groups += list(data.get("dependency-groups", {}).values())
    for group in groups:
        for spec in group:
            # "sqlalchemy[asyncio]>=2.0.36" -> "sqlalchemy"
            name = spec.split(";")[0].split("[")[0]
            for operator in (">=", "==", "<=", "~=", ">", "<", "!="):
                name = name.split(operator)[0]
            names.add(name.strip().lower().replace("_", "-"))
    return names


def _top_level_imports() -> dict[str, set[str]]:
    """Top-level module name -> the files importing it."""
    found: dict[str, set[str]] = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if any(part.startswith(".") or part == "alembic" for part in rel.parts):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # Relative imports are ours by definition.
                modules = [node.module] if node.module and node.level == 0 else []
            else:
                continue
            for module in modules:
                found.setdefault(module.split(".")[0], set()).add(str(rel))
    return found


def _third_party() -> dict[str, set[str]]:
    stdlib = sys.stdlib_module_names
    return {
        module: files
        for module, files in _top_level_imports().items()
        if module not in stdlib and module not in LOCAL_PACKAGES
    }


def _is_declared(module: str, declared: set[str]) -> bool:
    """Is this import name satisfied by a declared distribution?

    Exact match, or a namespace package whose implementation is split across
    several distributions — `import opentelemetry` is provided by
    opentelemetry-api, -sdk, -exporter-*, and so on, none of which is named
    plainly "opentelemetry".
    """
    name = DISTRIBUTION_NAMES.get(module, module).replace("_", "-")
    return name in declared or any(d.startswith(f"{name}-") for d in declared)


def test_every_imported_package_is_declared() -> None:
    declared = _declared()
    undeclared = {
        module: sorted(files)
        for module, files in _third_party().items()
        if not _is_declared(module, declared)
    }
    assert not undeclared, (
        "these are imported but not in pyproject.toml, so a fresh install would "
        f"fail: {undeclared}"
    )


def test_a_lockfile_is_committed() -> None:
    """Without one, two clones a month apart resolve different versions and a
    transitive break is unbisectable. It is also what Heroku's Python buildpack
    installs from."""
    assert (REPO / "uv.lock").exists(), "run `uv lock` and commit uv.lock"


def test_the_lockfile_covers_the_declared_dependencies() -> None:
    """A stale lock installs the wrong set — worse than none, because it looks
    authoritative."""
    lock = (REPO / "uv.lock").read_text()
    missing = [name for name in _declared() if f'name = "{name}"' not in lock]
    assert not missing, f"uv.lock is stale; re-run `uv lock`. Missing: {missing}"


@pytest.mark.parametrize(
    "module", ["opentelemetry", "prometheus_client", "mcp", "asyncpg", "greenlet"]
)
def test_runtime_critical_imports_are_declared(module: str) -> None:
    """Spot-checks for the ones whose absence fails at boot rather than at use.

    `greenlet` is the subtle one: nothing imports it directly, but SQLAlchemy's
    asyncio layer needs it at runtime and fails with a confusing error without it.
    """
    assert _is_declared(module, _declared()), module


def test_the_python_version_is_pinned_and_consistent() -> None:
    """Heroku's buildpack refuses to build without an explicit version, and
    without this file it defaulted to 3.14 — a different interpreter from the
    one everything here is tested against.

    The pin also has to agree with `requires-python` and ruff's target, or the
    linter enforces rules for one version while another actually runs.
    """
    pinned = (REPO / ".python-version").read_text().strip()
    assert pinned, ".python-version is empty"

    with (REPO / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)

    requires = data["project"]["requires-python"]
    assert pinned in requires or requires.startswith(f">={pinned}"), (
        f".python-version says {pinned} but requires-python says {requires}"
    )

    ruff_target = data["tool"]["ruff"]["target-version"]
    assert ruff_target == "py" + pinned.replace(".", ""), (
        f"ruff targets {ruff_target} but the runtime is {pinned}"
    )
