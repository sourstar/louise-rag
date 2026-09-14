"""Boxed from tests/hold/test_credit_kinds.py with the `packed` plugin
(2026-09-14). Reference only: nothing here is collected or run."""
from app.hold import template


def test_a_packed_collection_credits_the_ORIGINS_provider():
    """Packing does not make the text ours."""
    from app.plugins.packed.source import PackedSource
    packed = PackedSource(manifest={"plugin": "gutenberg", "license": {"class": "green"}})
    assert packed.corpus_provider == "Project Gutenberg"
    assert template.corpus_credit(packed).producer == "Project Gutenberg"
