"""tests/share/test_benchmarks_v2.py — a version-2 collection's benchmark
row (09 §9.9, amended 2026-10-07, operator's yes): its one statement names
the encoder's image, so the row carries `encoder_image` in place of
`encoder_engine`. Without it no version-2 collection ever had a benchmark,
and the second peer's contribute found "0 peers" for one."""
import time
from types import SimpleNamespace

from app.hold import envelope as env, runs
from app.share import coverage as cov, gossip, messages, peers
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import KEY, pubmed_v2
from tests.hold.test_vectors_build import _statement
from tests.share.test_messages import PEER_KEY

OWN = env.key_text(KEY.verify_key)


def test_the_row_comes_from_this_node_s_passing_statement_alone(tmp_path):
    codex = pubmed_v2()
    assert not runs.applies(codex), "a version-2 Codex: one statement, an image, no run"
    assert cov.v2_benchmark(tmp_path, "gutenberg", codex, OWN) is None, "no statement, no row"
    _statement(tmp_path, codex)
    row = cov.v2_benchmark(tmp_path, "gutenberg", codex, OWN)
    assert row == {"space_id": codex.space_id, "encoder_image": codex.space.encoder.image,
                   "encoder_weights": codex.space.encoder.weights, "tokens_per_s": 1000}
    assert cov.own_benchmarks(tmp_path, "gutenberg", codex, OWN) == [row]
    leading = SimpleNamespace(name="gutenberg", codex=codex, embed_mode_default="p2p", share=True)
    assert gossip.benchmarks(tmp_path, [leading], OWN) == [row], "the digest carries it"
    # 04 §4.5 (2026-10-20): only where this peer embeds under rendezvous AND shares
    for c in (SimpleNamespace(name="gutenberg", codex=codex, embed_mode_default="lazy", share=True),
              SimpleNamespace(name="gutenberg", codex=codex, embed_mode_default="off", share=True),
              SimpleNamespace(name="gutenberg", codex=codex, embed_mode_default="p2p", share=False),
              SimpleNamespace(name="gutenberg", codex=codex, embed_mode_default="all", share=False),
              SimpleNamespace(name="gutenberg", codex=codex)):
        assert not gossip.leads(c) and gossip.benchmarks(tmp_path, [c], OWN) == [], (c.embed_mode_default if hasattr(c, "embed_mode_default") else "default")
    assert gossip.leads(SimpleNamespace(name="gutenberg", codex=codex, embed_mode_default="all", share=True))
    assert cov.v2_benchmark(tmp_path, "gutenberg", codex, "ed25519:" + "0" * 64) is None, \
        "a statement another key signed is not this node's"
    _statement(tmp_path, codex, passed=0)
    assert cov.v2_benchmark(tmp_path, "gutenberg", codex, OWN) is None, "a failing statement is no benchmark"
    _statement(tmp_path, codex, space_id="sha256:" + "e" * 64)
    assert cov.v2_benchmark(tmp_path, "gutenberg", codex, OWN) is None, "about another space"


def test_a_neighbour_s_version_1_row_puts_it_in_the_live_set(node):  # noqa: F811
    """What 04 §4.5 reads of a row is its space and its rate, so the older
    form scores a peer exactly as the newer one does."""
    codex = pubmed_v2()
    now = time.time()
    row = {"space_id": codex.space_id, "encoder_image": codex.space.encoder.image,
           "encoder_weights": codex.space.encoder.weights, "tokens_per_s": 2436}
    entry = {"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "PubMed",
             "snapshot_id": "2026-10", "members": [{"name": "records-year=2024", "root": "sha256:" + "a" * 64,
                                                    "bytes": 1}], "license": "red", "privacy": "public"}
    with peers.connect() as con:
        body = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[],
                                    benchmarks=[row], collections=[entry], now=now)
        peers.record_digest(con, body, now=now)
        live = cov.live_peers(codex, con, now=now, own_key=OWN,
                              own_benchmarks=[{**row, "tokens_per_s": 96775}])
        assert sorted((p.key, p.benchmark) for p in live) == sorted([(PEER_KEY, 2436.0), (OWN, 96775.0)])
        # 04 §4.5 (2026-10-20): a neighbour that offers no member of the collection is not
        # scored, whatever benchmark its digest carries — it can publish no bundle
        for silent in ([], [dict(entry, members=[])]):
            body = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[],
                                        benchmarks=[row], collections=silent, now=now + 1)
            peers.record_digest(con, body, now=now + 1)
            live = cov.live_peers(codex, con, now=now + 1, own_key=OWN,
                                  own_benchmarks=[{**row, "tokens_per_s": 96775}])
            assert [p.key for p in live] == [OWN], silent
