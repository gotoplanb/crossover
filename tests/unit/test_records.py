"""Parsing Marvel records, including every quirk that has bitten someone."""

from __future__ import annotations

from datetime import date

from marvel.records import (
    PORTRAIT_UNCANNY,
    cover_url,
    issue_key,
    parse_comics,
    series_display_name,
    series_slug,
)


def test_series_slug_drops_the_year_range() -> None:
    """ "King in Black (2020 - 2021)" and "King In Black" have to agree, because
    curation YAML is written by a human and the API is not."""
    assert series_slug("King in Black (2020 - 2021)") == "king-in-black"
    assert series_slug("King In Black") == "king-in-black"
    assert series_slug("King in Black: Namor (2020 - 2021)") == "king-in-black-namor"


def test_issue_key_is_stable_across_both_forms() -> None:
    assert issue_key("King in Black (2020 - 2021)", 3) == issue_key("King in Black", 3)
    assert issue_key("King in Black", 3) == "king-in-black-3"


def test_series_display_name_strips_the_year_range() -> None:
    """Marvel's form is canonical and also not what anyone says out loud, so
    reader-facing titles use the stripped form."""
    assert series_display_name("King in Black (2020 - 2021)") == "King in Black"
    assert series_display_name("Venom (2018 - Present)") == "Venom"
    assert series_display_name("Venom") == "Venom"


def test_parsed_series_name_is_display_ready(event_comics_payload) -> None:
    """Guards a real bug: syncing used to rewrite "King in Black #1" as
    "King in Black (2020 - 2021) #1" everywhere it was shown."""
    records = {r.key: r for r in parse_comics(event_comics_payload)}
    assert records["king-in-black-1"].series_name == "King in Black"
    assert records["king-in-black-namor-1"].series_name == "King in Black: Namor"


def test_parse_extracts_the_four_id_spaces_separately(event_comics_payload) -> None:
    records = {r.key: r for r in parse_comics(event_comics_payload)}
    first = records["king-in-black-1"]
    assert first.digital_id == 55901
    assert first.marvel_com_issue_id == 86133
    assert first.marvel_api_comic_id == 86133  # same number here, different space
    # Gate C: the Branch sourceId is the marvel.com issue id, not the digital id.
    assert first.source_id == 86133
    assert first.source_id != first.digital_id


def test_digital_id_zero_means_absent(event_comics_payload) -> None:
    """Marvel returns 0, not null, for a comic with no digital edition."""
    records = {r.key: r for r in parse_comics(event_comics_payload)}
    assert records["king-in-black-2"].digital_id is None


def test_marvel_com_id_is_parsed_out_of_the_urls_array(event_comics_payload) -> None:
    """It is not a field of its own — it only appears inside a detail URL."""
    records = {r.key: r for r in parse_comics(event_comics_payload)}
    assert records["king-in-black-namor-1"].marvel_com_issue_id == 87001


def test_on_sale_date_is_picked_out_of_the_dates_array(event_comics_payload) -> None:
    records = {r.key: r for r in parse_comics(event_comics_payload)}
    # focDate is also present and must not win.
    assert records["king-in-black-1"].published_on == date(2020, 12, 2)


def test_characters_and_creators_flatten_to_names(event_comics_payload) -> None:
    records = {r.key: r for r in parse_comics(event_comics_payload)}
    assert records["king-in-black-1"].characters == ["Venom", "Knull"]
    assert "Donny Cates" in records["king-in-black-1"].creators


def test_cover_url_uses_the_requested_variant_and_upgrades_to_https() -> None:
    url = cover_url("http://i.annihil.us/u/prod/marvel/i/mg/6/03/abc", "jpg", PORTRAIT_UNCANNY)
    assert url == "https://i.annihil.us/u/prod/marvel/i/mg/6/03/abc/portrait_uncanny.jpg"


def test_marvel_placeholder_art_becomes_no_cover(event_comics_payload) -> None:
    """Marvel serves an `image_not_available` path; the rack draws its own
    missing-cover treatment instead of their grey box."""
    records = {r.key: r for r in parse_comics(event_comics_payload)}
    second = records["king-in-black-2"]
    assert cover_url(second.thumbnail_path, second.thumbnail_extension) is None


# --- malformed input from Marvel ---


def test_a_non_numeric_digital_id_is_treated_as_absent() -> None:
    """Gate B again: anything we cannot read as a real id must become None
    rather than something a reader URL could be built from."""
    from marvel.records import parse_comic

    record = parse_comic({"id": 1, "digitalId": "not-a-number", "issueNumber": 1})
    assert record.digital_id is None

    assert parse_comic({"id": 1, "digitalId": None, "issueNumber": 1}).digital_id is None
    assert parse_comic({"id": 1, "issueNumber": 1}).digital_id is None


def test_no_detail_url_means_no_marvel_com_id() -> None:
    from marvel.records import parse_comic

    record = parse_comic(
        {
            "id": 1,
            "issueNumber": 1,
            "urls": [{"type": "reader", "url": "http://marvel.com/digitalcomics/view/1"}],
        }
    )
    assert record.marvel_com_issue_id is None
    assert record.source_id is None


def test_missing_or_malformed_dates() -> None:
    from marvel.records import parse_comic

    # No onsaleDate at all.
    assert parse_comic({"id": 1, "issueNumber": 1, "dates": []}).published_on is None
    # Present but unparseable — must not raise.
    assert (
        parse_comic(
            {"id": 1, "issueNumber": 1, "dates": [{"type": "onsaleDate", "date": "soon"}]}
        ).published_on
        is None
    )
    # Marvel's sentinel for "unknown", which is a real value they send.
    assert (
        parse_comic(
            {
                "id": 1,
                "issueNumber": 1,
                "dates": [{"type": "onsaleDate", "date": "-0001-11-30T00:00:00-0500"}],
            }
        ).published_on
        is None
    )


def test_an_entirely_empty_record_parses() -> None:
    """Marvel's roster occasionally contains stubs. A crash here would take out
    a whole sync over one bad row."""
    from marvel.records import parse_comic

    record = parse_comic({})
    assert record.marvel_api_comic_id == 0
    assert record.series_name == ""
    assert record.digital_id is None
    assert record.characters == []


def test_parse_comics_handles_an_empty_envelope() -> None:
    from marvel.records import parse_comics

    assert parse_comics({}) == []
    assert parse_comics({"data": {}}) == []
    assert parse_comics({"data": {"results": []}}) == []


def test_cover_url_needs_both_halves() -> None:
    from marvel.records import cover_url

    assert cover_url(None, "jpg") is None
    assert cover_url("http://example/x", None) is None
    assert cover_url("", "") is None


def test_a_url_entry_with_no_issue_id_is_skipped_and_the_next_one_used() -> None:
    """Marvel puts several urls on a record; the detail link is not always
    first, so the loop has to keep looking rather than give up on entry one."""
    from marvel.records import parse_comic

    record = parse_comic(
        {
            "id": 1,
            "issueNumber": 3,
            "urls": [
                {"type": "reader", "url": "http://marvel.com/digitalcomics/view/999"},
                {"type": "inAppLink", "url": "http://marvel.com/comics/"},
                {"type": "detail", "url": "http://marvel.com/comics/issue/86135/kib_3"},
            ],
        }
    )
    assert record.marvel_com_issue_id == 86135


def test_a_dates_entry_of_another_type_is_skipped() -> None:
    """focDate and unlimitedDate come before onsaleDate on some records."""
    from datetime import date

    from marvel.records import parse_comic

    record = parse_comic(
        {
            "id": 1,
            "issueNumber": 1,
            "dates": [
                {"type": "focDate", "date": "2020-11-09T00:00:00-0500"},
                {"type": "unlimitedDate", "date": "2021-06-07T00:00:00-0500"},
                {"type": "onsaleDate", "date": "2020-12-02T00:00:00-0500"},
            ],
        }
    )
    assert record.published_on == date(2020, 12, 2)


def test_the_unlimited_date_is_parsed_separately_from_on_sale() -> None:
    """Two different questions. `onsaleDate` answers "does this exist";
    `unlimitedDate` answers "can I read it yet", and for a reader following an
    event as it comes out only the second one decides whether a link works."""
    from marvel.records import parse_comic

    record = parse_comic(
        {
            "id": 85649,
            "digitalId": 55807,
            "title": "King in Black (2020) #1",
            "issueNumber": 1,
            "series": {"name": "King in Black (2020 - 2021)"},
            "dates": [
                {"type": "onsaleDate", "date": "2020-12-02T00:00:00-0500"},
                {"type": "unlimitedDate", "date": "2021-03-01T00:00:00-0500"},
            ],
            "urls": [],
            "thumbnail": {},
        }
    )
    assert record.published_on.isoformat() == "2020-12-02"
    assert record.unlimited_on.isoformat() == "2021-03-01"


def test_a_record_with_no_unlimited_date_is_unknown_not_never() -> None:
    from marvel.records import parse_comic

    record = parse_comic(
        {
            "id": 1,
            "digitalId": 2,
            "title": "X (1990) #1",
            "issueNumber": 1,
            "series": {"name": "X (1990)"},
            "dates": [{"type": "onsaleDate", "date": "1990-01-01T00:00:00-0500"}],
            "urls": [],
            "thumbnail": {},
        }
    )
    assert record.unlimited_on is None
