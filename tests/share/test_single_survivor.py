"""tests/share/test_single_survivor.py — the single-survivor test of
migration step 9 (04 §4.2): every other peer gone, a fresh peer that knows
no one finds the survivor by computing the DHT key of the collection's
current snapshot, pulls the chain from it and the members from its swarm.

On localhost, with three real libtorrent sessions and a private DHT: A, the
survivor — the node's own process session, which the seeder announces and
serves through; N, one DHT node standing for the rest of the network; and
C, the newcomer, with no digest, no `[peers]` and no hint. The live DHT
could not stand in for it on 2026-10-07: behind the router, A's
announcement carries the public address, and nothing forwards that port
inward. What this proves is the path itself — the announce, the probe and
`dht_alerts`, without which the probe never heard an answer."""
import socket
import threading
import time

import pytest

lt = pytest.importorskip("libtorrent")

from fastapi.testclient import TestClient  # noqa: E402

from app import api, db  # noqa: E402
from app.acquire import seeding, transport_torrent as tt  # noqa: E402
from app.hold import collection as col, snapshot as sn  # noqa: E402
from app.hold.codex import canonical_json  # noqa: E402
from app.share import fetch, peers, seeder, transport  # noqa: E402
from tests.deposit.test_deposit_identity import node  # noqa: F401, E402
from tests.hold.conftest import KEY, NOW  # noqa: E402
from tests.share.test_fetch import fetcher  # noqa: F401, E402
from tests.share.test_seeder import SLICE, a_shared_collection  # noqa: E402
from tests.share.test_share_api import OURS, OUR_KEY  # noqa: E402
from tests.share.test_transport import manifest  # noqa: E402

#: a private DHT on one host: libtorrent keeps one node per IP and checks node
#: ids against addresses on the open network, neither of which holds here
DHT = {"enable_dht": True, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
       "dht_restrict_routing_ips": False, "dht_restrict_search_ips": False,
       "dht_prefer_verified_node_ids": False, "dht_ignore_dark_internet": False,
       "allow_multiple_connections_per_ip": True, "alert_mask": 0,
       # every node shares one address here, and a probe sends a query per key
       # and snapshot id at once: libtorrent's per-address rate limit blocked
       # the address for minutes after the first probe
       "dht_block_ratelimit": 100000}
WEB_PORT = 8742          # what A announces: the port its peer messages answer on


def _free_ports(n):
    socks = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def _dht_session(port, boot):
    return lt.session({**DHT, "listen_interfaces": f"127.0.0.1:{port}", "dht_bootstrap_nodes": boot})


def _meshed(sessions, ports, timeout=15.0):
    for s in sessions:
        for p in ports:
            s.add_dht_node(("127.0.0.1", p))
    deadline = time.time() + timeout
    while time.time() < deadline and not all(s.status().dht_nodes for s in sessions):
        time.sleep(0.2)


def test_a_newcomer_that_knows_no_one_finds_the_survivor_by_the_key_and_takes_the_collection(
        node, fetcher, monkeypatch):  # noqa: F811
    pa, pn, pc = _free_ports(3)
    monkeypatch.setattr(tt, "_session", None)
    monkeypatch.setattr(tt, "_session_lock", threading.Lock())
    monkeypatch.setattr(tt, "session_settings",
                        lambda: {**DHT, "listen_interfaces": f"127.0.0.1:{pa}",
                                 "dht_bootstrap_nodes": f"127.0.0.1:{pn}"})
    monkeypatch.setattr(seeding, "_seeder", None)
    monkeypatch.setattr(seeder, "_last_announce", {})
    # A, the survivor: a shared collection, its snapshot signed, served
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    (d / "snapshots/2026-08.sig.json").write_bytes(canonical_json(sn.sidecar_add(None, snap.hash, KEY, now=NOW)))
    a = tt.get_session()
    n = _dht_session(pn, f"127.0.0.1:{pa}")
    newcomer = _dht_session(pc, f"127.0.0.1:{pa},127.0.0.1:{pn}")
    _meshed([a, n, newcomer], [pa, pn, pc])
    con = db.connect(c)
    out = seeder.sync([c], {c.name: con}, con, "running", session=a, port=WEB_PORT)
    assert out["published"] == [c.name] and out["announced"] == 1, out   # a root: its lineage key is its collection key
    seeding.tick([c], {c.name: con}, con, "running")

    # C knows no one: no neighbour, no address in the peer list
    with peers.connect() as pcon:
        assert fetch.sources(pcon, codex.collection_id, now=time.time()) == []
    found = []
    for _ in range(3):                              # an announce takes a moment to settle
        found = fetch.find_dht(newcomer, codex.collection_id, wait_s=10.0)
        if found:
            break
        time.sleep(2)
    assert [s.address for s in found] == [f"http://127.0.0.1:{WEB_PORT}"], \
        "the survivor, found by the collection key of its current snapshot alone"

    # ... and the collection taken from it: the chain by its peer messages
    # (the test client answers at the address found), the members from its
    # swarm, found by the DHT too — no hint is given
    got = fetch.fetch(fetcher, codex.collection_id, session=newcomer, client=TestClient(api.app),
                      key=OURS, own_key=OUR_KEY)
    assert got["snapshots"]["promoted"] == ["2026-08"] and got["from"] == f"http://127.0.0.1:{WEB_PORT}"
    fname = got["name"]
    fcon = manifest(fetcher / fname / "state" / "manifest.sqlite")
    members = transport.fetch_members(fetcher, fname, codex, slices="all", kinds=("records",),
                                      ack_missing=False, session=newcomer, con=fcon, hints=[])
    assert sorted(members["promoted"]) == [f"records/{SLICE}/00001.jsonl.zst", f"records/{SLICE}/00002.jsonl.zst"], \
        members
    for k, data in files.items():
        assert (col.drag_of(fetcher, fname) / f"records/{SLICE}/{k:05d}.jsonl.zst").read_bytes() == data
