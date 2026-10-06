"""tests/share/test_peers.py — the peer list given by hand and its pins
(04 §4.2; 07 §7.3 `share peers add|show`; 07 §7.8 `[peers]`), the budget
every sent byte draws from first (04 §4.3), and the port a DHT
announcement names. Migration step 9c, second brick."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from nacl import signing

from app import api, louise, platform_config
from app.hold import envelope as env
from app.share import api as share_api, gossip, messages, peers
from app.transfer import schedule
from app.transfer.bucket import Priority, TransferAborted
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.share.test_messages import PEER, PEER_KEY
from tests.share.test_share_api import FakeClient

OTHER = signing.SigningKey(b"\x0b" * 32)
OTHER_KEY = env.key_text(OTHER.verify_key)
CID = "sha256:" + "0" * 64


def digest(key, address, *, collections=(), now=None):
    return messages.digest_body(key=key, address=address, neighbours=[], benchmarks=[],
                                collections=list(collections), now=now)


# ---- the peer list, in platform.toml ------------------------------------------------

def test_add_writes_the_peer_list_once_each_and_keeps_the_rest_of_the_file(node, capsys):  # noqa: F811
    path = platform_config.platform_path()
    path.write_text('[node]\nname = "nas"\n\n[peers]\naddresses = [\n  "http://old.test:8742",\n]\n'
                    '# a comment inside the section does not survive\n\n[dht]\nenabled = false\n',
                    encoding="utf-8")
    assert louise.main(["share", "peers", "add", "peer.test:8742"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["added"], out["address"], out["key"], out["peers"]) == (True, "http://peer.test:8742",
                                                                        None, 2)
    text = path.read_text(encoding="utf-8")
    assert text.startswith('[node]\nname = "nas"\n\n[peers]\n'), "where [peers] stood"
    assert text.endswith('\n\n[dht]\nenabled = false\n'), "the rest byte for byte"
    assert "a comment inside" not in text
    cfg = platform_config.load()
    assert cfg.peers.addresses == ("http://old.test:8742", "http://peer.test:8742")
    assert cfg.peers.dht is False and cfg.peers.name == "nas"
    before = text
    assert louise.main(["share", "peers", "add", "http://peer.test:8742/"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] is False
    assert path.read_text(encoding="utf-8") == before, "each address once; nothing rewritten"
    assert louise.main(["share", "peers", "add", "https://pinned.test", "--key", PEER_KEY]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["added"], out["pinned"], out["key"]) == (True, True, PEER_KEY)
    cfg = platform_config.load()
    assert cfg.peers.addresses[-1] == "https://pinned.test"
    assert cfg.peers.pins == {"https://pinned.test": PEER_KEY}
    assert peers.peer_list(cfg) == [{"address": "http://old.test:8742", "key": None},
                                    {"address": "http://peer.test:8742", "key": None},
                                    {"address": "https://pinned.test", "key": PEER_KEY}]
    assert peers.pinned_key("https://pinned.test/", cfg) == PEER_KEY
    assert peers.pinned_key("https://other.test", cfg) is None
    with peers.connect() as con:
        assert peers.recipients(con, now=time.time()) == list(cfg.peers.addresses), \
            "the list is where digests go, from the next beat"


def test_add_creates_the_file_when_there_is_none(node, capsys):  # noqa: F811
    path = platform_config.platform_path()
    assert not path.exists()
    assert louise.main(["share", "peers", "add", "http://peer.test:8742"]) == 0
    assert platform_config.load().peers.addresses == ("http://peer.test:8742",)
    assert path.read_text(encoding="utf-8").startswith("[peers]\naddresses = [")


@pytest.mark.parametrize("argv", [
    ["share", "peers", "add", "not an address"],
    ["share", "peers", "add", "http://peer.test/with/path"],
    ["share", "peers", "add", "ftp://peer.test"],
    ["share", "peers", "add", "http://peer.test:99999"],
    ["share", "peers", "add", "http://peer.test", "--key", "ed25519:zz"],
    ["share", "peers", "add", "http://peer.test", "--key", PEER_KEY[8:]],
])
def test_add_refuses_what_is_not_an_address_or_a_key(node, argv, capsys):  # noqa: F811
    assert louise.main(argv) == 2
    assert not platform_config.platform_path().exists()


def test_the_loader_normalises_addresses_and_refuses_a_pin_that_is_no_key(tmp_path):
    p = tmp_path / "platform.toml"
    p.write_text('[peers]\naddresses = ["peer.test:8742", "http://peer.test:8742/", "https://b.test"]\n'
                 f'pins = {{ "b.test" = "{PEER_KEY}" }}\n', encoding="utf-8")
    cfg = platform_config._load_one(p)
    assert cfg.peers.addresses == ("http://peer.test:8742", "https://b.test")
    assert cfg.peers.pins == {"http://b.test": PEER_KEY}, "a bare host:port is http"
    p.write_text('[peers]\naddresses = []\n[peers.pins]\n"http://a.test" = "nope"\n', encoding="utf-8")
    with pytest.raises(schedule.ScheduleError, match="pins"):
        platform_config._load_one(p)


# ---- show ------------------------------------------------------------------------------

def test_show_lists_the_peer_list_and_what_each_peer_holds(node, capsys):  # noqa: F811
    platform_config.platform_path().write_text(
        f'[peers]\naddresses = ["http://boot.test"]\n[peers.pins]\n"http://peer.test" = "{PEER_KEY}"\n',
        encoding="utf-8")
    now = time.time()
    entry = {"collection_id": CID, "lineage_id": CID, "title": "PubMed", "snapshot_id": "2026-08",
             "snapshot_hash": "sha256:" + "1" * 64, "codex_hash": "sha256:" + "2" * 64,
             "license": "green", "privacy": "public",
             "members": [{"name": "records-year=2023", "root": "sha256:" + "3" * 64, "bytes": 1}],
             "coverage": []}
    with peers.connect() as con:
        peers.record_digest(con, digest(PEER_KEY, "http://peer.test", collections=[entry], now=now),
                            now=now)
        old = now - 4 * peers.INTERVAL_S
        peers.record_digest(con, digest(OTHER_KEY, "http://other.test", now=old), now=old)
        peers.ban(con, OTHER_KEY, now=now, why="a lie")
    assert louise.main(["share", "peers", "show"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["peer_list"] == [{"address": "http://boot.test", "key": None},
                                {"address": "http://peer.test", "key": PEER_KEY}]
    by_key = {p["key"]: p for p in out["peers"]}
    p = by_key[PEER_KEY]
    assert (p["neighbour"], p["pinned"], p["banned_until"], p["address"]) == (
        True, True, None, "http://peer.test")
    assert p["last_digest"] == messages.time_text(now)
    assert p["collections"] == [{"collection_id": CID, "title": "PubMed", "snapshot_id": "2026-08",
                                 "license": "green", "privacy": "public", "members": 1}]
    o = by_key[OTHER_KEY]
    assert (o["neighbour"], o["pinned"], o["ban_why"], o["collections"]) == (False, False, "a lie", [])
    assert o["banned_until"] == messages.time_text(now + peers.BAN_S)
    assert out["events_seen"] == 0


# ---- pins at the door ---------------------------------------------------------------------

def test_a_digest_from_a_pinned_address_under_another_key_is_discarded_and_logged(node, monkeypatch):  # noqa: F811
    platform_config.platform_path().write_text(
        f'[peers]\naddresses = ["http://peer.test:8742"]\n[peers.pins]\n"http://peer.test:8742" = "{PEER_KEY}"\n',
        encoding="utf-8")
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(share_api, "_log", lambda level, text: logged.append((level, text)))
    client = TestClient(api.app)
    r = client.post("/api/share/digest/share",
                    json=env.sign(env.DIGEST, digest(OTHER_KEY, "http://peer.test:8742"), OTHER))
    assert r.status_code == 400 and "pinned" in r.json()["detail"]
    assert len(logged) == 1 and logged[0][0] == "warning" and OTHER_KEY in logged[0][1]
    with peers.connect() as con:
        assert peers.peer(con, OTHER_KEY) is None, "discarded: not even seen"
    r = client.post("/api/share/digest/share",
                    json=env.sign(env.DIGEST, digest(PEER_KEY, "http://peer.test:8742/"), PEER))
    assert r.status_code == 200, "the pinned key, the same address spelt with a slash"
    r = client.post("/api/share/digest/share",
                    json=env.sign(env.DIGEST, digest(OTHER_KEY, "http://other.test"), OTHER))
    assert r.status_code == 200, "the other key at its own address is only itself"
    assert len(logged) == 1


# ---- the budget -------------------------------------------------------------------------

class FakeBucket:
    def __init__(self):
        self.draws: list[tuple[int, Priority]] = []

    def acquire(self, n, priority, stop=None):
        if stop is not None and stop.is_set():
            raise TransferAborted("stopped")
        self.draws.append((n, priority))


class FakeManager:
    def __init__(self):
        self.up, self.down, self.refreshed = FakeBucket(), FakeBucket(), 0

    def maybe_refresh(self):
        self.refreshed += 1

    def bucket(self, direction):
        from app.transfer.manager import Direction
        return self.up if direction is Direction.UP else self.down


def test_every_byte_sent_is_drawn_from_the_up_bucket_first_and_a_stop_ends_the_round(node, monkeypatch):  # noqa: F811
    from app.transfer import manager as tm
    fake_mgr = FakeManager()
    monkeypatch.setattr(tm, "get_manager", lambda: fake_mgr)
    monkeypatch.setattr(gossip, "_last_digest_at", time.time())     # no digest: forwarding alone
    now = time.time()
    with peers.connect() as con:
        for key, address in ((PEER_KEY, "http://peer.test"), (OTHER_KEY, "http://other.test")):
            peers.record_digest(con, digest(key, address, now=now), now=now)
        body = messages.event_body(key=PEER_KEY, type_="bundle", payload={"x": 1}, now=now)
        peers.mark_seen(con, env.body_id(body), now=now, time_text=body["time"], type_="bundle",
                        hops=7, envelope=env.sign(env.EVENT, body, PEER))
    fake = FakeClient()
    stop = threading.Event()
    stop.set()
    assert gossip.beat([], now=now, client=fake, stop=stop)["forwarded"] == 0, "a stop ends the round"
    assert fake.posts == [] and fake_mgr.up.draws == []
    with peers.connect() as con:
        assert len(peers.to_forward(con)) == 1, "left for the next beat, not lost"
    stop.clear()
    assert gossip.beat([], now=now, client=fake, stop=stop)["forwarded"] == 1
    assert [u for u, _ in fake.posts] == ["http://other.test/api/share/event/share"]
    assert fake.bytes > 0 and fake_mgr.up.draws == [(fake.bytes, Priority.DELTA)]
    assert fake_mgr.down.draws == [] and fake_mgr.refreshed == 2, "the schedule is read per send"
    assert gossip.PRIORITY is Priority.DELTA


# ---- the announced port -------------------------------------------------------------------

def test_the_digest_names_the_swarm_port_the_file_gives_and_none_otherwise(node, monkeypatch):  # noqa: F811
    from app.acquire import transport_torrent as tt
    monkeypatch.setattr(tt, "_session", None)
    p = platform_config.platform_path()
    assert gossip.peer_port() is None, "no session, no [node] peer_port: no swarm to name"
    p.write_text('[node]\npeer_port = 6881\n', encoding="utf-8")
    assert gossip.peer_port() == 6881
    body = messages.digest_body(key=PEER_KEY, address="http://a", neighbours=[], benchmarks=[],
                                collections=[], peer_port=gossip.peer_port())
    assert body["peer_port"] == 6881 and "peer_port" not in digest(PEER_KEY, "http://a")
    p.write_text('[node]\npeer_port = 0\n', encoding="utf-8")
    with pytest.raises(schedule.ScheduleError, match="peer_port"):
        platform_config.load()


def test_the_announced_port_is_the_one_peers_reach(node, monkeypatch):  # noqa: F811
    from app import config
    monkeypatch.setattr(config, "PORT", 8742)
    p = platform_config.platform_path()
    assert gossip.own_port() == 8742, "no [node] address: the process's own"
    p.write_text('[node]\naddress = "http://192.168.178.10:30042"\n', encoding="utf-8")
    assert gossip.own_port() == 30042
    assert gossip.own_address() == "http://192.168.178.10:30042"
    p.write_text('[node]\naddress = "https://louise.example.org"\n', encoding="utf-8")
    assert gossip.own_port() == 443
