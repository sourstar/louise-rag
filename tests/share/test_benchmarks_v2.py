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
    assert gossip.benchmarks(tmp_path, [SimpleNamespace(name="gutenberg", codex=codex)], OWN) == [row], \
        "the digest carries it"
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
    with peers.connect() as con:
        body = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[],
                                    benchmarks=[row], collections=[], now=now)
        peers.record_digest(con, body, now=now)
        live = cov.live_peers(codex, con, now=now, own_key=OWN,
                              own_benchmarks=[{**row, "tokens_per_s": 96775}])
    assert sorted((p.key, p.benchmark) for p in live) == sorted([(PEER_KEY, 2436.0), (OWN, 96775.0)])
