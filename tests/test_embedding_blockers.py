"""What stops a collection from embedding, said in words (CLAUDE.md RULE 2).

2026-11-06: wikidata's embedding was turned on in the Library and nothing
happened for hours. The collection was paused on its own — the pause carried
over by `hold collection rename` — and no surface said so: the switch
answered ok, the card showed nothing, `hold collection new` ended on "the
gate PASSED". `node_state.embedding_blockers` is what each of them asks now.
"""
import sqlite3
import time
from contextlib import closing

from app import db, node_state


def _paused_at(name: str) -> str:
    at = [h for h in node_state.pause_history() if h["collection"] == name][-1]["at"]
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at))


def test_nothing_blocks_a_running_node_and_a_collection_set_to_embed():
    node_state.set_worker_state("running")
    for embed in ("lazy", "all", "p2p", None):
        assert node_state.embedding_blockers("wd", embed) == [], embed


def test_a_mode_that_does_not_embed_is_said_with_what_lifts_it():
    for state, label in (("paused", "pause"), ("acquire-only", "download/seeding only"),
                         ("seed-only", "seeding only")):
        node_state.set_worker_state(state)
        (said,) = node_state.embedding_blockers("wd", "all")
        assert f'The node is in "{label}" mode ({node_state.MODE_SAYS[state]})' in said, said
        assert '"run everything"' in said and "`louise node resume`" in said, said


def test_a_paused_collection_names_who_paused_it_and_when_from_its_latest_entry():
    node_state.set_worker_state("running")
    node_state.set_paused("wd", True, by="louise node pause wd")
    node_state.set_paused("wd", False, by="louise node resume wd")
    node_state.set_paused("wd", True, by="rename wikidata -> wd")
    node_state.set_paused("other", True, by="someone else")
    (said,) = node_state.embedding_blockers("wd", "all")
    assert said.startswith(f'wd is paused on its own, by "rename wikidata -> wd" on {_paused_at("wd")}: '), said
    assert "Resume it in the Library" in said and "`louise node resume wd`" in said, said
    assert node_state.pause_of("wd")["by"] == "rename wikidata -> wd"
    node_state.set_paused("wd", False, by="louise node resume wd")
    assert node_state.embedding_blockers("wd", "all") == [], "another collection's pause is not this one's"
    assert node_state.pause_of("wd") is None


def test_a_pause_nothing_recorded_is_still_said():
    """A pause written before the history was kept (2026-10-31)."""
    node_state.set_worker_state("running")
    with closing(node_state.connect()) as c:
        db.set_control(c, node_state.PAUSED_KEY, '["wd"]')
    (said,) = node_state.embedding_blockers("wd", "all")
    assert "wd is paused on its own (nothing recorded who paused it, or when)" in said, said
    assert node_state.pause_of("wd") == {"by": None, "at": None, "when": None}


def test_the_embed_setting_off_is_said_and_none_leaves_it_out():
    node_state.set_worker_state("running")
    (said,) = node_state.embedding_blockers("wd", "off")
    assert said.startswith('wd\'s embed setting is "off"') and "--set embed=" in said, said
    assert node_state.embedding_blockers("wd", None) == []


def test_every_blocker_at_once_in_order():
    node_state.set_worker_state("acquire-only")
    node_state.set_paused("wd", True, by="rename wikidata -> wd")
    said = node_state.embedding_blockers("wd", "off")
    assert [s.split(" ")[:3] for s in said] == [["The", "node", "is"], ["wd", "is", "paused"],
                                                 ["wd's", "embed", "setting"]], said


def test_a_held_connection_is_read_as_the_store_is():
    node_state.set_worker_state("running")
    node_state.set_paused("wd", True, by="louise node pause wd")
    with closing(node_state.connect()) as c:
        (said,) = node_state.embedding_blockers("wd", "all", con=c)
        assert node_state.pause_of("wd", con=c)["by"] == "louise node pause wd"
    assert said.startswith('wd is paused on its own, by "louise node pause wd"'), said


def test_an_absent_store_reads_as_the_worker_reads_it_and_is_not_created():
    assert not node_state.path().exists()
    (said,) = node_state.embedding_blockers("wd", "all")
    assert said.startswith('The node is in "pause" mode'), said
    assert not node_state.path().exists(), "a reader creates nothing"


def test_an_unreadable_store_is_said_as_unreadable_and_never_raises():
    p = node_state.path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not a database, " * 64)
    said = node_state.embedding_blockers("wd", "off")
    assert len(said) == 2, said
    assert said[0].startswith(f"The node's state ({p}) could not be read") and "`louise node show`" in said[0]
    assert said[1].startswith('wd\'s embed setting is "off"'), "what is known is still said"
    assert node_state.pause_of("wd") is None
    gone = sqlite3.connect(":memory:")
    gone.close()
    (said,) = node_state.embedding_blockers("wd", "all", con=gone)
    assert "could not be read" in said, said
