"""Cached responses from the metadata mirror.

Deliberately **not** `marvel_response_cache`, despite the near-identical shape.
That table is the Gate B evidence store: `cached_record_index` reads every row
in it and runs `parse_comics` over each body, so anything living there is
asserting "this is a Marvel API record of that issue".

Mirror responses fail that on both counts. Their search bodies are
`{query, count, items}` rather than Marvel's `{data: {results}}` envelope, so
the parse would find nothing; and a mirror record is a different provenance
class, which `Issue.digital_id_source` exists precisely to keep distinguishable.
Sharing one table would quietly weaken the evidence store to save a migration.

Disposable in the same way: keyed by path + normalized query, refetchable, never
hand-edited. Dropping this table costs request budget, not correctness.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class MirrorResponseCache(Base):
    __tablename__ = "mirror_response_cache"
    __table_args__ = (
        UniqueConstraint("path", "params_key", name="uq_mirror_cache_path_params"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Path below the mirror's /v1, no leading slash — "issues/8164",
    #: "search/issues", "series/2002/issues".
    path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    #: Query params sorted and rendered canonically. No auth triple to strip
    #: here — the mirror takes no credentials.
    params_key: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
