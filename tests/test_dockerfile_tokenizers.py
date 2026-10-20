"""Every tokenizer a chunker cuts by is in the platform image, pinned.

The `wikidata-text` chunker loads `<TOKENIZERS_DIR>/<hex>.json` and its
error said "the platform image fetches it at build" — no Dockerfile step
did, and 506 wikidata documents were errored on the node (2026-10-20). A
bind-mounted test run never sees the image, so this reads the Dockerfile:
each tokenizer digest the repository pins (the recipe's layer, the fetch
script) is ADDed under /opt/louise/tokenizers/<hex>.json and checked by
sha256sum in the same image."""
import re
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parent.parent
PINS = (ROOT / "docs" / "notes" / "wikidata-recipe.toml", ROOT / "deploy" / "jina-embeddings-v3" / "fetch.py")


def _pinned_tokenizers() -> set[str]:
    out = set()
    for path in PINS:
        text = path.read_text(encoding="utf-8")
        out |= set(re.findall(r'tokenizer\s*=\s*"sha256:([0-9a-f]{64})"', text))
        out |= set(re.findall(r'TOKENIZER_SHA256\s*=\s*"([0-9a-f]{64})"', text))
    return out


def test_every_pinned_tokenizer_is_added_and_checked_in_the_image():
    pinned = _pinned_tokenizers()
    assert pinned, "the wikidata recipe pins its tokenizer"
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base = docker.split("FROM node:", 1)[0]          # the runtime stage, not the test-only ones
    assert str(config.TOKENIZERS_DIR) == "/opt/louise/tokenizers"
    for hex_ in pinned:
        target = f"/opt/louise/tokenizers/{hex_}.json"
        assert re.search(rf"^ADD https://\S+ {re.escape(target)}$", base, re.M), f"no ADD of {target}"
        assert re.search(rf'"{hex_}  {re.escape(target)}"\s*\\?\s*\n?\s*\| sha256sum -c -', base), \
            f"{target} is not checked against its digest"
