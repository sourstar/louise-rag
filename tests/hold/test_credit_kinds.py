"""tests/hold/test_credit_kinds.py — whom a collection is owed to, by kind.

02 §2.7 and 09 §9.3, amended 2026-09-09 on the operator's decision: credit
the Project Louise community AND the corpus provider for the part the
community embedded.

Neither had anywhere to go before. `credits` held pre-embedded archives
only; `license` is `{class, terms_sha256}` and `terms_sha256` hashes the
licence TEXT, which for CC BY-SA says credit must be given without naming
who is owed it; `origin` is a locator. So the community-embedded layer of
a Wikipedia collection named nobody — and a CORRECTIVE SISTER of it would
have carried every word of Wikipedia's text with `credits: []`.

The fix is a `kind` on every entry, and the kind is not decoration: it
says by which rule the entry descends, and which of the six keys mean
anything. The three lifetimes are what this file is about.
"""
from dataclasses import replace

import pytest

from app.hold import codex as cx, template
from app.hold.codex import Certifier, CodexError, Credit
from tests.hold.conftest import (a_community_credit, a_corpus_credit,
                                 a_credit, with_credits)

CERT = Certifier("ed25519:" + "5c" * 32, "https://gazzetta.example/.well-known/louise",
                 True)


def sorted_credits(*entries) -> tuple:
    """09 §9.3's order, so a test aiming at one refusal does not trip the
    sort check on the way."""
    return tuple(sorted(entries, key=cx.credit_order))


def certified(codex, **over):
    """A certified collection, which is the only kind whose `embedding` may
    be anything but `"open"` (03 §3.8)."""
    return replace(codex, publication="certified", certifier=CERT,
                   content="manifest-only", succession={"open-after": "P2Y"},
                   **over)


# ---- the corpus entry is on every collection ------------------------------

def test_a_codex_without_a_corpus_credit_is_refused(codex):
    """The one credit no collection can be without: every corpus came from
    someone. `credits` is therefore never empty."""
    with pytest.raises(CodexError, match='exactly one kind="corpus" entry'):
        cx.validate(replace(codex, credits=(a_community_credit(),)))


def test_two_corpus_credits_are_refused(codex):
    other = replace(a_corpus_credit(), producer="somebody else")
    with pytest.raises(CodexError, match='exactly one kind="corpus" entry'):
        cx.validate(replace(codex, credits=sorted_credits(
            a_community_credit(), a_corpus_credit(), other)))


def test_a_green_plugin_declares_a_corpus_credit_too():
    """06 §6.1: every plugin, green ones included. A provider that asks for
    nothing is still the provider, and a rule with an exception is a rule
    someone lands on the wrong side of."""
    gutenberg = template.source_for("gutenberg")
    entry = template.corpus_credit(gutenberg)
    assert entry.kind == "corpus"
    assert entry.producer == "Project Gutenberg"
    assert entry.license == "public-domain"
    assert entry.archive is None and entry.coverage == "all"


def test_a_plugin_that_declares_no_provider_cannot_make_a_collection():
    """The failure belongs at the plugin, not at someone's `create`."""
    class Mute:
        source_id = "mute"
    with pytest.raises(template.TemplateError, match="declares no corpus_provider"):
        template.corpus_credit(Mute())


# ---- the community entry is DERIVED, never declared -----------------------

def test_the_community_is_credited_exactly_when_contributed_vectors_are_taken(codex):
    """Present when `embedding` is "open" or "endorsed", absent when it is
    "certifier-only". Derived from a field the Codex already carries, so
    there is nothing to declare and nothing to drop."""
    plain = replace(codex, embedding="open", credits=with_credits())
    cx.validate(plain)
    assert [x.kind for x in plain.credits] == ["community", "corpus"]

    # "endorsed" needs a certified collection: an open one's embedding is
    # open (03 §3.8). It still takes contributed vectors, so it still owes.
    endorsed = certified(codex, embedding="endorsed", credits=with_credits())
    cx.validate(endorsed)
    assert [x.kind for x in endorsed.credits] == ["community", "corpus"]


def test_an_open_collection_that_credits_no_community_is_refused(codex):
    with pytest.raises(CodexError, match='carries kind="community" entry'):
        cx.validate(replace(codex, embedding="open",
                            credits=(a_corpus_credit(),)))


def test_a_certifier_only_collection_that_credits_a_community_is_refused(codex):
    """It takes no contributed vectors, so there is nobody to credit for
    them — and a credit that is always there whatever the collection does
    says nothing."""
    closed = certified(codex, embedding="certifier-only", credits=with_credits())
    with pytest.raises(CodexError, match='carries no kind="community" entry'):
        cx.validate(closed)
    # ...and it is valid the moment the entry goes.
    cx.validate(replace(closed, credits=(a_corpus_credit(),)))


def test_the_community_is_one_collective_and_never_a_roster(codex):
    """§2.7's distinction. A roster is unbounded and changing and belongs in
    the coverage map (§2.5); hashing one into `collection_id` would fix at
    creation a membership nobody knows then and which is never fixed."""
    peer = replace(a_community_credit(), producer="peer-7f3a")
    with pytest.raises(CodexError, match='at most one kind="community" entry'):
        cx.validate(replace(codex, credits=sorted_credits(
            a_community_credit(), peer, a_corpus_credit())))


def test_the_community_entry_names_no_licence(codex):
    """Contributed vectors carry the collection's own terms; there is no
    second licence to record."""
    licensed = replace(a_community_credit(), license="apache-2.0")
    with pytest.raises(CodexError, match=r"credits\[community\].license is null"):
        cx.validate(replace(codex, credits=sorted_credits(
            a_corpus_credit(), licensed)))


# ---- only an archive entry names an archive, or a range -------------------

def test_only_an_archive_entry_names_an_archive(codex):
    named = replace(a_corpus_credit(), archive="upstash-wikipedia-2024-06")
    with pytest.raises(CodexError, match=r"credits\[corpus\].archive is null"):
        cx.validate(replace(codex, credits=sorted_credits(
            named, a_community_credit())))


def test_only_an_archive_entry_declares_a_range(codex):
    """Coverage on the other kinds is `"all"` — a provider and the community
    are owed for every document — and 06 §6.3 reads none of them."""
    ranged = replace(a_corpus_credit(),
                     coverage={"ids_per_shard": 1000, "shards": 2})
    with pytest.raises(CodexError, match=r'credits\[corpus\].coverage is "all"'):
        cx.validate(replace(codex, credits=sorted_credits(
            ranged, a_community_credit())))


def test_an_archive_entry_is_credited_under_a_licence(codex):
    bad = replace(a_credit(), license=None)
    with pytest.raises(CodexError, match="credited under a"):
        cx.validate(replace(codex, credits=with_credits(bad)))


# ---- the sort, which is what keeps two creators on one collection_id ------

def test_credits_sort_by_kind_then_archive_then_producer(codex):
    """`credits` is hashed into `collection_id`, so two creators building the
    same collection must produce the same list in the same order or they
    have made two collections out of one."""
    entries = with_credits(a_credit("zenodo"), a_credit("cohere"))
    assert [(c.kind, c.archive) for c in entries] == [
        ("archive", "cohere"), ("archive", "zenodo"),
        ("community", None), ("corpus", None)]
    cx.validate(replace(codex, credits=entries))

    shuffled = (entries[3], entries[0], entries[2], entries[1])
    with pytest.raises(CodexError, match="sorted by .kind, archive, producer"):
        cx.validate(replace(codex, credits=shuffled))


# ---- what reads which kind ------------------------------------------------

def test_the_archive_key_refusal_reads_only_archive_entries(codex, monkeypatch):
    """03 §3.5 resolves a `producer_pubkey` to an archive through the
    registry and then refuses the bundle unless `credits` names that
    archive. The corpus and community entries name no archive and gate
    nothing — the community entry is precisely the standing acknowledgement
    of the peers whose keys the registry does NOT know.
    """
    monkeypatch.setattr(template, "archive_of_key", lambda k: "cohere")
    without = replace(codex, credits=with_credits())
    assert not template.credits_admit(without, "ed25519:whoever")

    with_it = replace(codex, credits=with_credits(a_credit("cohere")))
    assert template.credits_admit(with_it, "ed25519:whoever")

    # A key the registry does not know belongs to no archive: an ordinary
    # peer's bundle, admitted or refused by the ladder like any other.
    monkeypatch.setattr(template, "archive_of_key", lambda k: None)
    assert template.credits_admit(without, "ed25519:a-peer")


def test_a_corpus_credit_does_not_swallow_every_document_into_one_layer():
    """The invariant-1 trap this design could have set. A corpus entry
    carries `coverage: "all"` because a provider is owed for everything —
    and 06 §6.3's `archive:<id>` rule must not read it, or every document
    lands in the archive layer, is cut by the wrong chunker, and produces
    different vectors under a `space_id` two peers agree on.
    """
    from dataclasses import dataclass, field
    from app import layers as layers_mod
    from app.layers import Layer

    @dataclass
    class Coll:
        layers: tuple = ()
        credits: tuple = ()
        plugin: str = "wikipedia"

    wiki = "upstash-wikipedia-2024-06"
    two = (Layer("verbatim", 1, f"archive:{wiki}"),
           Layer("cdc", 1, "default", {"target": 4000, "min": 1000, "max": 8000}))

    # The corpus and community entries alone: the archive covers nothing
    # here, so every document falls to the default layer.
    bare = Coll(layers=two, credits=with_credits())
    for nid in (0, 5, 10 ** 9):
        assert layers_mod.layer_of(bare, nid).chunker == "cdc"

    # Add the archive entry and the split appears — from the ARCHIVE
    # entry's coverage, and from nothing else.
    covered = Coll(layers=two, credits=with_credits(
        Credit(kind="archive", archive=wiki, producer="Upstash",
               license="CC-BY-SA-4.0", attribution="…",
               coverage={"ids_per_shard": 1000, "shards": 2})))
    assert layers_mod.layer_of(covered, 1999).chunker == "verbatim"
    assert layers_mod.layer_of(covered, 2000).chunker == "cdc"


def test_show_prints_every_kind_not_only_the_archives(tmp_path, codex):
    """A reader asking whom this collection stands on is owed the corpus
    provider and the community too, which is the entire reason those
    entries exist."""
    from app.hold import collection as col
    col.create(tmp_path, replace(codex, credits=with_credits(a_credit())))
    shown = col.show(tmp_path, codex.plugin)
    assert [c["kind"] for c in shown["credits"]] == \
        ["archive", "community", "corpus"]
    assert any(c["producer"] == "the Project Louise community"
               for c in shown["credits"])


# ---- what `create` assembles ---------------------------------------------

def test_build_credits_takes_each_kind_from_its_own_source():
    """A creator writes none of the three: the corpus entry from the plugin,
    the archive entries from the registry, the community entry from
    `embedding` (06 §6.6)."""
    gutenberg = template.source_for("gutenberg")
    archives = (a_credit("cohere"),)

    open_ = template.build_credits(gutenberg, archives, "open")
    assert [c.kind for c in open_] == ["archive", "community", "corpus"]

    closed = template.build_credits(gutenberg, archives, "certifier-only")
    assert [c.kind for c in closed] == ["archive", "corpus"]

    none = template.build_credits(gutenberg, (), "open")
    assert [c.kind for c in none] == ["community", "corpus"]


def test_a_recipe_cannot_write_a_corpus_or_community_credit():
    """02 §2.7: a creator free to phrase an attribution is a creator free to
    phrase it away. Neither kind is a recipe field at all — `archives` is
    the only credit-shaped thing a recipe carries."""
    assert "credits" not in template.OPTIONAL
    assert "corpus_provider" not in template.OPTIONAL
    assert "archives" in template.OPTIONAL


def test_a_packed_collection_credits_the_ORIGINS_provider():
    """Packing does not make the text ours."""
    from app.plugins.packed.source import PackedSource
    packed = PackedSource(manifest={"plugin": "gutenberg", "license": {"class": "green"}})
    assert packed.corpus_provider == "Project Gutenberg"
    assert template.corpus_credit(packed).producer == "Project Gutenberg"
