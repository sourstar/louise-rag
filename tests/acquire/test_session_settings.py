"""tests/acquire/test_session_settings.py — the one libtorrent session's
settings (app/acquire/transport_torrent.py), and that libtorrent takes them."""
import pytest

from app.acquire import transport_torrent as tt

lt = pytest.importorskip("libtorrent")


def test_several_peers_behind_one_address_may_all_connect():
    """2026-10-07: every container on Docker Desktop, like every node behind
    one router, shares one address — and libtorrent's default of one
    connection per IP per torrent refused the second peer's calibration
    member right after the handshake."""
    s = tt.session_settings()
    assert s["allow_multiple_connections_per_ip"] is True
    assert s["enable_upnp"] is False and s["enable_natpmp"] is False, "no hole punched in a router unasked"
    s["alert_mask"] = 0
    session = lt.session({**s, "listen_interfaces": "127.0.0.1:0"})
    assert session.get_settings()["allow_multiple_connections_per_ip"] is True


def test_the_platform_file_s_dht_switch_is_the_session_s(monkeypatch):
    """`[dht] enabled` was parsed and read by nothing (2026-10-07)."""
    from types import SimpleNamespace
    from app import platform_config
    monkeypatch.delenv("TORRENT_DHT", raising=False)
    monkeypatch.setattr(platform_config, "load", lambda: SimpleNamespace(peers=SimpleNamespace(dht=False)))
    assert tt.session_settings()["enable_dht"] is False
    monkeypatch.setattr(platform_config, "load", lambda: SimpleNamespace(peers=SimpleNamespace(dht=True)))
    assert tt.session_settings()["enable_dht"] is True
    monkeypatch.setenv("TORRENT_DHT", "0")
    assert tt.session_settings()["enable_dht"] is False, "the variable still overrides when set"


def test_the_dht_s_answers_are_delivered_while_a_reader_waits_and_only_then():
    session = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                          "alert_mask": int(lt.alert.category_t.storage_notification)})
    before = session.get_settings()["alert_mask"]
    dht = int(lt.alert.category_t.dht_notification) | int(lt.alert.category_t.dht_operation_notification)
    with tt.dht_alerts(session):
        assert session.get_settings()["alert_mask"] & dht == dht
    assert session.get_settings()["alert_mask"] == before
    with tt.dht_alerts(object()):          # a fake without settings is left alone
        pass
