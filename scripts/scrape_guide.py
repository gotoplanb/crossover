"""Scrape a marvel.com reading guide into curation YAML seed rows (SPEC §5.2).

Marvel's own guides are a better seed for ordering than any third-party list.
They are not available through the API, so this is a scraper.

**Not wired into any automated path.** marvel.com returns 403 to plain
programmatic requests (docs/gates.md), so this must be run from an environment
with a real browser session — copy the page HTML to a file and pass `--html`.
Output is printed for hand-review, never written straight into a curation file:
a guide is a seed, and the ordering corrections are the curation work.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from marvel.records import issue_key

#: Marvel's guide markup lists each issue as a link to /comics/issue/{id}/{slug}
#: with the title in the anchor text.
#:
#: Split into two passes rather than one big pattern. A single regex with two
#: unbounded `[^"]*` groups around a literal is the polynomial-backtracking
#: shape SonarQube's S5852 flags, and on a whole downloaded HTML page that is
#: not merely theoretical. Matching the anchor once and then testing its href
#: separately keeps every quantifier bounded by one attribute.
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?href="(?P<href>[^"]{1,500})"[^>]*>(?P<title>[^<]{3,120})</a>',
    re.IGNORECASE,
)
_ISSUE_HREF_RE = re.compile(r"/comics/issue/(\d+)(?:/|$)")
_TITLE_RE = re.compile(r"^(?P<series>[^#]{1,200}?) *# *(?P<number>\d{1,5})$")


def parse_guide(html: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for match in _ANCHOR_RE.finditer(html):
        issue_id = _ISSUE_HREF_RE.search(match.group("href"))
        if issue_id is None:
            # A series or character link, not an issue.
            continue
        title = " ".join(match.group("title").split())
        parsed = _TITLE_RE.match(title)
        if not parsed:
            continue
        series = parsed.group("series").strip()
        number = int(parsed.group("number"))
        key = issue_key(series, number)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "key": key,
                "series": series,
                "number": number,
                # The marvel.com issue id, which Gate C found doubles as the
                # Branch sourceId. Not a digital_id and never usable as one.
                "marvel_com_issue_id": int(issue_id.group(1)),
            }
        )
    return out


def to_yaml(entries: list[dict]) -> str:
    lines = ["issues:"]
    for position, entry in enumerate(entries, start=1):
        lines += [
            f"  - key: {entry['key']}",
            f"    position: {position}",
            f'    series: "{entry["series"]}"',
            f"    number: {entry['number']}",
            f"    marvel_com_issue_id: {entry['marvel_com_issue_id']}",
            "    role: optional_tie_in",
            "    narrative_role: parallel",
            "    franchise: other",
            "    provisional: true",
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, required=True, help="saved guide page HTML")
    args = parser.parse_args(argv)

    entries = parse_guide(args.html.read_text())
    if not entries:
        print("No issues found. Marvel's markup may have changed.", file=sys.stderr)
        return 1
    print(f"# {len(entries)} issues scraped. Roles, narrative roles, franchises and")
    print("# ordering corrections are all still hand work — this is a seed only.")
    print(to_yaml(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
