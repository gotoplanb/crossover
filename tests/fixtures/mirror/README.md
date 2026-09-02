# Recorded mirror responses

Real responses from the third-party Marvel metadata mirror, captured once and
replayed ever since. **Not hand-authored** — that is the entire point.

## Why they exist

The mirror allows 60 requests a minute, per IP, and that budget is shared with
whatever else uses the same address (see issue #28). Two things were spending it
for no return:

- **Reseeding.** Rebuilding one 40-issue snapshot costs about 80 requests, so it
  cannot finish inside a single window. Iterating on curation meant paying that
  repeatedly. `make snapshot` can now run `--offline` for free.
- **Tests asserting invented shapes.** The suite never touched the network, but
  its payloads were dicts written by hand. They prove the parser is
  self-consistent, not that it reads what the mirror actually sends — so a
  change in the real format would leave every test green.

## Using them

    make record-mirror                       # capture the default query set
    python -m scripts.record_mirror --query "Thor 700"
    python -m scripts.fetch_snapshot king-in-black --record    # then --offline

Re-running skips anything already recorded, so adding one query costs one query.

Nothing in the app or the test suite records. A test that could reach the mirror
would spend the shared budget on every CI run.

## Reading a diff

Filenames are readable on purpose (`search-issues-q-daredevil-limit-100-<hash>.json`)
so a change shows which query moved without decoding anything. The hash only
breaks ties between truncated names.

A response body changing is worth looking at rather than waving through: these
are the record of what the mirror really returns, and `tests/unit/
test_mirror_real_shapes.py` is what fails when that drifts.
