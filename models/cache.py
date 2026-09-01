"""The raw Marvel response cache (SPEC §3).

Disposable by construction: keyed by endpoint + normalized params, refetchable
at any time, and *nothing hand-edited ever lives here*. If this table were
dropped, `make sync-event` rebuilds it.

It has a second job beyond politeness to Marvel's rate limit: it is the
evidence store for Gate B. A `digital_id` is only legitimate if some row here
contains it, for that same issue, from a real API response. That check is a
test (SPEC §8), and this table is what it reads.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class MarvelResponseCache(Base):
    __tablename__ = "marvel_response_cache"
    __table_args__ = (
        UniqueConstraint("endpoint", "params_key", name="uq_cache_endpoint_params"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # e.g. "events/860/comics" — path below /v1/public, no leading slash.
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Params sorted and rendered canonically, with the auth triple (ts/apikey/
    # hash) stripped: those change every call and would defeat the cache.
    params_key: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    etag: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    # Marvel's required attribution string, captured from the response itself
    # rather than hardcoded, since their terms tie it to the data.
    attribution_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
