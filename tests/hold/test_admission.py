"""tests/hold/test_admission.py — 08 §8.2's aggregate limit.

Migration plan step 4g: what replaces trust on first use. The clock is
injected everywhere, because a budget tested against the wall clock is a
budget tested at one point in the hour.
"""
import pytest

from app import platform_config
from app.hold import admission as adm

KEY_A = "ed25519:" + "aa" * 32
KEY_B = "ed25519:" + "bb" * 32
KEY_C = "ed25519:" + "cc" * 32
T0 = 1_756_713_600


@pytest.fixture
def con(tmp_path):
    (tmp_path / "collections").mkdir()
    return adm.connect(tmp_path / "collections")


def _offer(con, key, n, *, at, limits=None, ref="sha256:00"):
    return adm.offer(con, key, n, kind="bundle", ref=ref,
                     limits=limits or adm.Limits(), now=at)


# ---- where the state lives -----------------------------------------------

def test_the_node_state_is_beside_the_collections_never_inside_one(tmp_path):
    """07 §7.8: `state/` is a sibling of `collections/`. Inside one it
    would sit in the SHARD a peer serves, and this is exactly what no peer
    sees."""
    root = tmp_path / "collections"
    assert adm.state_dir(root) == tmp_path / "state"
    root.mkdir()
    adm.connect(root).close()
    assert (tmp_path / "state" / "node.sqlite").is_file()
    assert not list(root.rglob("node.sqlite"))


# ---- 08 §8.1: no countermeasure needs knowing whom to trust --------------

def test_a_first_seen_key_is_admitted(con):
    """The whole regression this step exists for. `app/bundles/trust.py`
    pins the first key a collection sees and refuses every later one; 08
    §8.1 forbids exactly that, and calls per-sender limiting too weak
    besides — "minting a fresh key is free for the attacker, while
    keeping per-sender state costs the defender"."""
    assert _offer(con, KEY_A, 1000, at=T0) == adm.ADMITTED
    assert _offer(con, KEY_B, 1000, at=T0) == adm.ADMITTED
    assert _offer(con, KEY_C, 1000, at=T0) == adm.ADMITTED


def test_a_thousand_fresh_keys_fill_the_queue_no_faster_than_one(con):
    """§8.2 in as many words: "the scarce resource being protected is
    unverified novelty, not identity"."""
    lim = adm.Limits(bytes_per_hour=10_000, objects_per_hour=1000)
    one = [_offer(con, KEY_A, 1000, at=T0, limits=lim) for _ in range(15)]
    assert one.count(adm.ADMITTED) == 10
    assert one.count(adm.QUEUED) == 5
    # …and the next test spends the same budget across fifteen keys.


def test_the_same_budget_is_spent_by_many_keys_as_by_one(tmp_path):
    """The other half of the sentence above, on a fresh peer so the two
    runs are comparable."""
    lim = adm.Limits(bytes_per_hour=10_000, objects_per_hour=1000)
    (tmp_path / "collections").mkdir()
    con = adm.connect(tmp_path / "collections")
    keys = [f"ed25519:{i:064x}" for i in range(15)]
    got = [_offer(con, k, 1000, at=T0, limits=lim) for k in keys]
    assert got.count(adm.ADMITTED) == 10 and got.count(adm.QUEUED) == 5


# ---- the two allowances --------------------------------------------------

def test_the_object_count_binds_before_the_bytes_do(con):
    """"64 MB and one thousand objects per hour, whichever binds first —
    an attestation is a few hundred bytes, so a million of them fit inside
    any byte budget and would swamp a catalogue by count alone.\""""
    lim = adm.Limits(bytes_per_hour=64 * 1024 ** 2, objects_per_hour=3)
    got = [_offer(con, KEY_A, 200, at=T0, limits=lim) for _ in range(5)]
    assert got == [adm.ADMITTED] * 3 + [adm.QUEUED] * 2
    assert adm.spent(con, now=T0) == (600, 3)


def test_the_bytes_bind_before_the_count_does(con):
    lim = adm.Limits(bytes_per_hour=2_500, objects_per_hour=1000)
    got = [_offer(con, KEY_A, 1_000, at=T0, limits=lim) for _ in range(4)]
    assert got == [adm.ADMITTED] * 2 + [adm.QUEUED] * 2


def test_the_window_moves_on(con):
    """"per unit time": an hour later the allowance is whole again."""
    lim = adm.Limits(bytes_per_hour=2_000, objects_per_hour=1000)
    assert _offer(con, KEY_A, 2_000, at=T0, limits=lim) == adm.ADMITTED
    assert _offer(con, KEY_A, 2_000, at=T0 + 60, limits=lim) == adm.QUEUED
    assert _offer(con, KEY_A, 2_000, at=T0 + adm.WINDOW_S + 1,
                  limits=lim) == adm.ADMITTED


def test_a_peer_that_lowers_them_to_zero_accepts_nothing_from_strangers(con):
    """§8.2 calls that "complete in every other respect" — a
    configuration, not a broken node."""
    lim = adm.Limits(bytes_per_hour=0, objects_per_hour=0)
    assert lim.closed
    assert _offer(con, KEY_A, 1, at=T0, limits=lim) == adm.QUEUED
    adm.record_reproduced(con, KEY_B, ref="sha256:ab", now=T0)
    assert _offer(con, KEY_B, 1, at=T0, limits=lim) == adm.KNOWN


# ---- the ledger ----------------------------------------------------------

def test_material_from_a_reproduced_key_is_never_queued(con):
    """§8.2: "Material from a key whose work this peer has seen reproduced
    is never queued." It does not spend the allowance either, because the
    allowance is defined over what is UNVERIFIED — and §8.2 defines that
    as material "signed by keys none of whose work this peer has seen
    reproduced"."""
    lim = adm.Limits(bytes_per_hour=1, objects_per_hour=1)
    adm.record_reproduced(con, KEY_A, ref="sha256:ab", chunks=300, now=T0)
    for _ in range(50):
        assert _offer(con, KEY_A, 10 ** 9, at=T0, limits=lim) == adm.KNOWN
    assert adm.spent(con, now=T0) == (0, 0)


def test_the_ledger_counts_bundles_and_chunks(con):
    adm.record_reproduced(con, KEY_A, ref="sha256:01", chunks=300, now=T0)
    adm.record_reproduced(con, KEY_A, ref="sha256:02", chunks=200, now=T0 + 5)
    row = adm.reproduced(con, KEY_A)
    assert row["bundles"] == 2 and row["chunks"] == 500
    assert row["first_seen"] == T0 and row["last_seen"] == T0 + 5
    assert row["last_ref"] == "sha256:02"
    assert adm.reproduced(con, KEY_B) is None


# ---- the queue -----------------------------------------------------------

def test_the_queue_is_bounded_and_drops_the_NEWEST(con):
    """"When it is full the arriving object is refused rather than an
    older one evicted; otherwise a flooder empties the queue of legitimate
    material simply by arriving later, which is the cheapest attack there
    is." And "a refusal is not a judgement about the sender and is not
    remembered\"."""
    lim = adm.Limits(bytes_per_hour=0, objects_per_hour=0, queue_bytes=3_000)
    assert _offer(con, KEY_A, 1_000, at=T0, limits=lim, ref="a") == adm.QUEUED
    assert _offer(con, KEY_A, 1_000, at=T0, limits=lim, ref="b") == adm.QUEUED
    assert _offer(con, KEY_B, 1_000, at=T0, limits=lim, ref="c") == adm.QUEUED
    assert _offer(con, KEY_C, 1, at=T0, limits=lim, ref="d") == adm.REFUSED

    kept = [r["ref"] for r in con.execute(
        "SELECT ref FROM admission_queue ORDER BY id")]
    assert kept == ["a", "b", "c"], "an older object was evicted for a newer"
    assert adm.queued_bytes(con) == 3_000
    # not remembered: room appears, the same object is offered again
    lim2 = adm.Limits(bytes_per_hour=10, objects_per_hour=1, queue_bytes=4_000)
    assert _offer(con, KEY_C, 1, at=T0, limits=lim2, ref="d") == adm.ADMITTED


def test_the_queue_releases_in_arrival_order_and_stops_at_what_does_not_fit(con):
    """A queue that reordered itself by size would let a flood of small
    objects starve a legitimate large one indefinitely."""
    closed = adm.Limits(bytes_per_hour=0, objects_per_hour=0)
    for ref, n in (("a", 100), ("big", 5_000), ("c", 100)):
        assert _offer(con, KEY_A, n, at=T0, limits=closed, ref=ref) == adm.QUEUED

    lim = adm.Limits(bytes_per_hour=1_000, objects_per_hour=1000)
    out = adm.release(con, limits=lim, now=T0 + adm.WINDOW_S + 1)
    assert [r["ref"] for r in out] == ["a"], "it skipped the big one for c"
    assert [r["ref"] for r in con.execute(
        "SELECT ref FROM admission_queue ORDER BY id")] == ["big", "c"]

    wide = adm.Limits(bytes_per_hour=100_000, objects_per_hour=1000)
    out = adm.release(con, limits=wide, now=T0 + adm.WINDOW_S + 1)
    assert [r["ref"] for r in out] == ["big", "c"]


def test_a_closed_peer_releases_nothing(con):
    closed = adm.Limits(bytes_per_hour=0, objects_per_hour=0)
    _offer(con, KEY_A, 100, at=T0, limits=closed, ref="a")
    assert adm.release(con, limits=closed, now=T0 + 10_000) == []


# ---- the numbers are the peer's ------------------------------------------

def test_platform_toml_carries_the_admission_block(tmp_path, monkeypatch):
    """07 §7.8's line, parsed. §8.2: "Both allowances and the queue's
    depth are set in `platform.toml`; the figures above are the
    defaults\"."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "platform.toml").write_text(
        '[admission]\nbytes_per_hour = "64MB"\nobjects_per_hour = 1000\n'
        'queue = "256MB"\n', encoding="utf-8")
    c = platform_config.load().admission
    assert c.bytes_per_hour == 64 * 1000 ** 2
    assert c.objects_per_hour == 1000
    assert c.queue_bytes == 256 * 1000 ** 2


def test_the_defaults_are_8_2s(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    c = platform_config.load().admission
    assert (c.bytes_per_hour, c.objects_per_hour) == (64 * 1024 ** 2, 1000)
    assert adm.Limits().bytes_per_hour == c.bytes_per_hour


def test_zero_is_a_setting_and_not_an_absence(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "platform.toml").write_text(
        "[admission]\nbytes_per_hour = 0\nobjects_per_hour = 0\n",
        encoding="utf-8")
    c = platform_config.load().admission
    assert c.bytes_per_hour == 0 and c.objects_per_hour == 0


@pytest.mark.parametrize("text,want", [
    ("64MB", 64 * 1000 ** 2), ("64MiB", 64 * 1024 ** 2), ("512", 512),
    ("1GB", 1000 ** 3), ("2kb", 2000), ("64 MB", 64 * 1000 ** 2)])
def test_sizes_parse(tmp_path, monkeypatch, text, want):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "platform.toml").write_text(
        f'[admission]\nbytes_per_hour = "{text}"\n', encoding="utf-8")
    assert platform_config.load().admission.bytes_per_hour == want


def test_an_unknown_unit_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    from app.transfer import schedule as schedule_mod
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "platform.toml").write_text(
        '[admission]\nbytes_per_hour = "64 furlongs"\n', encoding="utf-8")
    with pytest.raises(schedule_mod.ScheduleError, match="unknown size unit"):
        platform_config.load()
