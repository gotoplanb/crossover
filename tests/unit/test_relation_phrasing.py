"""Every reference edge type must read as a sentence.

The reference graph's value is the explanation it carries, so an edge type with
no phrasing is a curation feature that silently renders as "related to". This
also guards a real crash: a missing map entry raised KeyError and 500'd the
whats_next payload and the rack.
"""

from __future__ import annotations

import pytest

from models.types import RelationType
from service.guide import relation_phrase


@pytest.mark.parametrize("relation", list(RelationType), ids=lambda r: r.value)
def test_every_relation_type_has_a_phrase(relation: RelationType) -> None:
    phrase = relation_phrase(relation.value)
    assert phrase != "related to", (
        f"{relation.value} has no entry in _RELATION_PHRASING, so it renders as the "
        "generic fallback"
    )
    assert phrase.islower(), "phrases are sentence fragments, not headings"


def test_an_unknown_relation_type_does_not_raise() -> None:
    """A page that 500s because someone added an enum member is worse than a
    generic phrase."""
    assert relation_phrase("not_a_real_relation") == "related to"
    assert relation_phrase("") == "related to"
