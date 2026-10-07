"""tests/share/test_answered.py — the web's answers to peers' pulls under the
node's one UP budget (invariant 5; app/share/answered.py): paced by the web
process's own bucket, owed in state/node.sqlite, charged to the worker's
bucket at its next gossip beat."""
import inspect

from app.share import answered
from app.transfer.bucket import TokenBucket
from app.transfer.manager import Direction
from tests.deposit.test_deposit_identity import node  # noqa: F401


class Manager:
    def __init__(self):
        self.up = TokenBucket(1_000_000, name="up")
        self.down = TokenBucket(None, name="down")

    def bucket(self, direction):
        return self.up if direction is Direction.UP else self.down


def test_an_answer_is_paced_owed_and_charged_once_to_the_worker(node, monkeypatch):  # noqa: F811
    import app.transfer.manager as tm
    web, worker = Manager(), Manager()
    monkeypatch.setattr(tm, "get_manager", lambda: web)
    answered.pace(4_000)
    answered.pace(1_000)
    assert sum(web.up.granted_snapshot()[1]) == 5_000, "the web's own bucket paced both answers"
    monkeypatch.setattr(tm, "get_manager", lambda: worker)
    before = worker.up.core.tokens
    assert answered.drain() == 5_000
    assert worker.up.core.tokens <= before - 5_000 + 1, "charged to the worker's tokens: one budget"
    assert sum(worker.up.granted_snapshot()[1]) == 5_000
    assert answered.drain() == 0, "charged once"


def test_the_pull_route_paces_its_answer_and_the_beat_drains():
    from app.share import api, gossip
    assert "answered.pace" in inspect.getsource(api._pull), "every pull answer goes through the pacing"
    assert "answered.drain" in inspect.getsource(gossip.beat), "and the worker's beat charges it"
