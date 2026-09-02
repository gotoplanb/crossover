"""The refetch/curation ownership split (SPEC §3).

"A refetch must never be able to clobber hand-curation" is the single most
important structural rule in the project. It is enforced by a column list
rather than by discipline, and this is the test that keeps the list honest.
"""

from __future__ import annotations

from datetime import date

from marvel.records import ComicRecord
from marvel.sync import (
    API_OWNED_COLUMNS,
    CURATION_OWNED_COLUMNS,
    apply_record,
    promote_availability,
)
from models.catalog import Issue
from models.types import Availability


def test_the_two_ownership_sets_are_disjoint() -> None:
    overlap = API_OWNED_COLUMNS & CURATION_OWNED_COLUMNS
    assert overlap == set(), f"columns owned by both layers: {overlap}"


def test_curated_columns_are_not_writable_by_a_refetch() -> None:
    """Order, roles and availability are curation's alone. If one of these ever
    appears in API_OWNED_COLUMNS, a sync can silently undo curation work."""
    for column in ("availability", "provisional", "unavailable_note", "key"):
        assert column not in API_OWNED_COLUMNS


def _record(**kwargs) -> ComicRecord:
    defaults = dict(
        marvel_api_comic_id=1,
        series_name="King in Black (2020 - 2021)",
        series_slug="king-in-black",
        issue_number=3,
        title="King in Black (2020) #3",
        published_on=date(2021, 1, 27),
        digital_id=55903,
        marvel_com_issue_id=86135,
        thumbnail_path="http://example/cover",
        thumbnail_extension="jpg",
    )
    return ComicRecord(**{**defaults, **kwargs})


def test_apply_record_leaves_curated_columns_alone() -> None:
    issue = Issue(
        key="king-in-black-3",
        series_name="",
        series_slug="",
        issue_number=3,
        availability=Availability.UNAVAILABLE.value,
        unavailable_note="checked by hand, not on MU",
        provisional=True,
    )
    apply_record(issue, _record())

    assert issue.digital_id == 55903
    assert issue.source_id == 86135
    # Untouched by the sync:
    assert issue.availability == Availability.UNAVAILABLE.value
    assert issue.unavailable_note == "checked by hand, not on MU"
    assert issue.provisional is True
    assert issue.key == "king-in-black-3"


def test_a_sparse_record_does_not_erase_known_values() -> None:
    """A thin record from a search endpoint must not blank out what a richer
    event fetch already established."""
    issue = Issue(key="k", series_name="King in Black", series_slug="king-in-black", issue_number=3)
    apply_record(issue, _record())
    apply_record(issue, _record(digital_id=None, thumbnail_path=None, thumbnail_extension=None))
    assert issue.digital_id == 55903
    assert issue.thumbnail_path == "http://example/cover"


def test_promote_availability_only_moves_on_a_real_id() -> None:
    issue = Issue(
        key="k",
        series_name="s",
        series_slug="s",
        issue_number=1,
        availability=Availability.UNCONFIRMED.value,
    )
    assert promote_availability(issue) is False
    assert issue.availability == Availability.UNCONFIRMED.value

    issue.digital_id = 42
    assert promote_availability(issue) is True
    assert issue.availability == Availability.LINKABLE.value


def test_a_sync_never_overrides_a_curated_unavailable() -> None:
    """ "this genuinely isn't on Marvel Unlimited" is a statement of fact a
    curator made; a sync has no standing to argue with it."""
    issue = Issue(
        key="k",
        series_name="s",
        series_slug="s",
        issue_number=1,
        availability=Availability.UNAVAILABLE.value,
        digital_id=99,
    )
    assert promote_availability(issue) is False
    assert issue.availability == Availability.UNAVAILABLE.value


def test_linkable_falls_back_when_an_id_disappears() -> None:
    """If Marvel drops a digital edition, the issue has to stop claiming a link
    rather than keep a dangling one."""
    issue = Issue(
        key="k",
        series_name="s",
        series_slug="s",
        issue_number=1,
        availability=Availability.LINKABLE.value,
        digital_id=None,
    )
    assert promote_availability(issue) is True
    assert issue.availability == Availability.UNCONFIRMED.value


def test_the_unlimited_date_is_api_owned() -> None:
    """Refetchable Marvel data, not a curation judgement — so a sync may write
    it and curation may not (SPEC §3)."""
    from marvel.sync import API_OWNED_COLUMNS

    assert "unlimited_on" in API_OWNED_COLUMNS
