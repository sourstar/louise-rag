"""rsync traffic reaches the per-class bandwidth accounting.

The Network page's "Bandwidth by priority class" read zero whenever rsync
was the thing moving bytes — which on the reference node is gutenberg, the
biggest mover. Streaming transports draw per chunk from the token bucket
(counted); rsync is a subprocess paced by a rate lease, and its bytes never
touched the counters. Now the transport polls its growing .part file each
second and credits the delta (bucket.note_granted): pure accounting, no
token math — pacing stays the lease's job.
"""
import time

from app.acquire import budget
from app.acquire.spec import Endpoint, RemoteFile, Transport
from app.acquire.transport_rsync import RsyncTransport
from app.transfer.bucket import Priority, TokenBucket


def test_note_granted_counts_without_touching_tokens():
    bucket = TokenBucket(rate_bps=1000.0)
    _, before = bucket.granted_snapshot()
    tokens_before = bucket.core.tokens
    bucket.note_granted(5000, Priority.BACKFILL)
    _, after = bucket.granted_snapshot()
    assert after[Priority.BACKFILL] - before[Priority.BACKFILL] == 5000
    assert bucket.core.tokens == tokens_before, \
        "accounting must not double-charge the budget the lease already paid"


def test_local_rsync_fetch_is_accounted(tmp_path, monkeypatch, isolated_config):
    """A real (local-path) rsync fetch must report its bytes. Local rsync
    needs no daemon, so this exercises the genuine subprocess + .part flow."""
    src = tmp_path / "origin"
    src.mkdir()
    payload = b"z" * 200_000
    (src / "book.txt").write_bytes(payload)

    noted = []
    monkeypatch.setattr(budget, "note_external_bytes", lambda n: noted.append(n))
    # the reservation would consult the manager singleton; keep it inert
    from contextlib import contextmanager

    @contextmanager
    def no_reservation(endpoint_key=None):
        yield None
    monkeypatch.setattr(budget, "rsync_reservation", no_reservation)

    dest = tmp_path / "corpus" / "book.txt"
    endpoint = Endpoint(transport=Transport.RSYNC, url=str(src))
    size = RsyncTransport().fetch_one(endpoint, RemoteFile(rel_path="book.txt"),
                                      dest, 0)
    assert size == len(payload) and dest.read_bytes() == payload
    assert sum(noted) == len(payload), \
        f"every landed byte must be accounted, got {sum(noted)} of {len(payload)}"


def test_accounted_bytes_reach_the_per_class_rates(isolated_config, monkeypatch):
    """End to end into rates(): what note_granted counts, the Network page
    sees as that class's rate on the next sample."""
    from app.transfer import manager as manager_mod
    monkeypatch.setattr(manager_mod, "_manager", None)   # fresh singleton
    m = manager_mod.get_manager()
    m.rates()                                            # first sample: baseline
    m.bucket(manager_mod.Direction.DOWN).note_granted(300_000, Priority.BACKFILL)
    time.sleep(1.1)                                      # rates cache + elapsed
    payload = m.rates()
    assert payload["down"]["backfill"] > 0, payload["down"]
    monkeypatch.setattr(manager_mod, "_manager", None)
