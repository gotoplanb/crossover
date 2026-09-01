"""Guard the SonarQube source enumeration.

`sonar.sources` has to be an explicit list, because Sonar refuses to index a
file as both source and test and a flat layout makes `sonar.sources=.` overlap
`sonar.tests=tests`. The cost of an explicit list is that a new top-level module
can silently escape static analysis — which is the kind of gap nobody notices
until a security hotspot goes unreported for months.

So this test is the tripwire: add a module, forget the properties file, and the
suite tells you.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PROPERTIES = REPO / "sonar-project.properties"

#: Top-level entries that are deliberately not Python source roots.
NOT_SOURCE = {
    "tests",           # declared as sonar.tests
    "alembic",         # migration harness; versions/ are generated
    "templates",       # Jinja, not Python
    "static",
    "docs",
    "curation/events",  # data, not code
}


def _property(name: str) -> list[str]:
    """Read one (possibly line-continued) property as a list of values."""
    text = PROPERTIES.read_text()
    # Join backslash continuations so multi-line values read as one.
    joined = text.replace("\\\n", "")
    for line in joined.splitlines():
        if line.startswith(f"{name}="):
            raw = line.split("=", 1)[1]
            return [v.strip() for v in raw.split(",") if v.strip()]
    raise AssertionError(f"{name} not found in sonar-project.properties")


def _on_disk_source_roots() -> set[str]:
    roots: set[str] = set()
    for entry in REPO.iterdir():
        if entry.name.startswith(".") or entry.name in NOT_SOURCE:
            continue
        is_package = entry.is_dir() and (entry / "__init__.py").exists()
        is_module = entry.is_file() and entry.suffix == ".py"
        if is_package or is_module:
            roots.add(entry.name)
    return roots


def test_every_python_module_is_declared_as_a_sonar_source() -> None:
    declared = set(_property("sonar.sources"))
    missing = _on_disk_source_roots() - declared
    assert not missing, (
        f"these would not be analyzed by SonarQube: {sorted(missing)}. "
        "Add them to sonar.sources in sonar-project.properties."
    )


def test_no_declared_source_has_been_deleted() -> None:
    """A stale entry makes the scanner warn and hides real drift in the noise."""
    stale = {s for s in _property("sonar.sources") if not (REPO / s).exists()}
    assert not stale, f"sonar.sources lists paths that no longer exist: {sorted(stale)}"


def test_sources_and_tests_are_disjoint() -> None:
    """The exact condition that made the scanner fail with 'can't be indexed twice'."""
    sources = set(_property("sonar.sources"))
    tests = set(_property("sonar.tests"))
    assert not (sources & tests)
    for test_root in tests:
        assert test_root not in sources
        assert "." not in sources, (
            "sonar.sources='.' subsumes sonar.tests and makes the scan fail"
        )


def test_coverage_exclusions_match_the_coverage_omit_list() -> None:
    """Sonar's coverage exclusions and pyproject's `omit` must agree.

    If they drift, Sonar's coverage percentage and the local one disagree, and
    the pre-push gate starts arguing with the quality gate over the same code.
    """
    import tomllib

    with (REPO / "pyproject.toml").open("rb") as fh:
        omit = set(tomllib.load(fh)["tool"]["coverage"]["run"]["omit"])
    sonar = set(_property("sonar.coverage.exclusions"))

    def normalize(patterns: set[str]) -> set[str]:
        out = set()
        for p in patterns:
            p = p.rstrip("/")
            for suffix in ("/**", "/*"):
                if p.endswith(suffix):
                    p = p[: -len(suffix)]
            out.add(p)
        return out

    # .venv and tests are omitted locally but already fully excluded from Sonar
    # analysis, so they have no business in a *coverage* exclusion list.
    local = normalize(omit) - {".venv", "tests"}
    assert local == normalize(sonar), (
        f"coverage exclusions disagree — pyproject omits {sorted(local)}, "
        f"sonar excludes {sorted(normalize(sonar))}"
    )
