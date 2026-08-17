"""Bundle CLI verbs. The security-relevant assertion is the first one: keygen
must never print the private seed."""
import os

import numpy as np
import pytest
from nacl import signing

from app import cli, collections as collections_mod, embeddings
from app.bundles import keys as bundle_keys, ledger, trust
from tests.bundles.helpers import seed_document

UID = "gutenberg:1"


@pytest.fixture
def wired(con, collection, monkeypatch):
    monkeypatch.setattr(cli.collections_mod, "load_all", lambda: [collection])
    return collection


def test_keygen_never_prints_the_seed(tmp_path, capsys):
    path = tmp_path / "node.key"
    assert cli.main(["bundle", "keygen", str(path)]) == 0
    out = capsys.readouterr().out
    seed = path.read_bytes()
    assert len(seed) == 32
    assert seed.hex() not in out, "the private seed was printed"
    public = bundle_keys.public_key_hex(signing.SigningKey(seed))
    assert public in out, "the public key should be shown — it is the producer identity"


def test_keygen_sets_restrictive_permissions(tmp_path):
    path = tmp_path / "node.key"
    cli.main(["bundle", "keygen", str(path)])
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
        bundle_keys.load_signing_key(path)      # must not raise


def test_keygen_refuses_to_overwrite(tmp_path):
    path = tmp_path / "node.key"
    cli.main(["bundle", "keygen", str(path)])
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        cli.main(["bundle", "keygen", str(path)])


def test_export_then_import_round_trip(wired, con, tmp_path, capsys, monkeypatch):
    collection = wired
    rows = seed_document(con, collection, UID, 1, n_chunks=4, seed=1)
    key_path = tmp_path / "node.key"
    cli.main(["bundle", "keygen", str(key_path)])
    from app import config
    monkeypatch.setattr(config, "NODE_KEY_PATH", key_path)

    dest = tmp_path / "out"
    assert cli.main(["bundle", "export", collection.name, str(dest)]) == 0
    assert (dest / "header.json").is_file()
    capsys.readouterr()

    con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (UID,))
    by_text = {r["text"]: np.asarray(r["vector"], dtype=np.float32) for r in rows}
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda batch: [by_text[t] for t in batch])

    assert cli.main(["bundle", "import", collection.name, str(dest)]) == 0
    out = capsys.readouterr().out
    assert "imported" in out and "first use" in out


def test_import_of_a_broken_bundle_exits_nonzero(wired, con, tmp_path, capsys,
                                                  producer_key, monkeypatch):
    from app.bundles import export as bundle_export
    collection = wired
    seed_document(con, collection, UID, 1, n_chunks=4, seed=1)
    dest = tmp_path / "out"
    bundle_export.export_bundle(con, collection, [UID], dest, signing_key=producer_key)
    sig = dest / "MANIFEST.sig"
    data = bytearray(sig.read_bytes())
    data[0] ^= 0x01
    sig.write_bytes(bytes(data))

    assert cli.main(["bundle", "import", collection.name, str(dest)]) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_trust_and_quarantine_verbs(wired, con, capsys):
    collection = wired
    pubkey = "a" * 64
    assert cli.main(["bundle", "trust", collection.name, pubkey]) == 0
    assert trust.check_admission(con, pubkey) == "trusted"

    capsys.readouterr()
    assert cli.main(["bundle", "trust", collection.name, pubkey,
                     "--quarantine", "--reason", "bad vectors"]) == 0
    assert "quarantined" in capsys.readouterr().out
    with pytest.raises(trust.BundleUntrusted):
        trust.check_admission(con, pubkey)


def test_quarantine_reports_which_documents_to_rebuild(wired, con, capsys):
    """An operator needs to know what to re-embed, not just that a key is
    banned."""
    collection = wired
    pubkey = "b" * 64
    ledger.record_applying(con, bundle_id="b1", producer_pubkey=pubkey,
                            space_id="s" * 32, collection=collection.name,
                            chunk_count=5, signature_hex="f" * 128,
                            parquet_sha256="1" * 64, content_digest="2" * 64,
                            created_at=0, space_weak=False)
    ledger.record_applied(con, "b1", {"gutenberg:1": 3, "gutenberg:2": 2}, "")
    cli.main(["bundle", "trust", collection.name, pubkey, "--quarantine"])
    out = capsys.readouterr().out
    assert "gutenberg:1: 3" in out and "gutenberg:2: 2" in out


def test_status_shows_producers_and_bundles(wired, con, capsys):
    collection = wired
    trust.trust_key(con, "c" * 64)
    ledger.record_applying(con, bundle_id="b9", producer_pubkey="c" * 64,
                            space_id="s" * 32, collection=collection.name,
                            chunk_count=2, signature_hex="f" * 128,
                            parquet_sha256="1" * 64, content_digest="2" * 64,
                            created_at=0, space_weak=False)
    ledger.record_applied(con, "b9", {"gutenberg:1": 2}, "t0=ok")
    assert cli.main(["bundle", "status", collection.name]) == 0
    out = capsys.readouterr().out
    assert "cccccccccccccccc" in out and "trusted" in out
    assert "applied" in out


def test_export_without_a_key_fails_cleanly(wired, con, tmp_path, capsys, monkeypatch):
    from app import config
    collection = wired
    seed_document(con, collection, UID, 1, n_chunks=2, seed=1)
    monkeypatch.setattr(config, "NODE_KEY_PATH", None)
    assert cli.main(["bundle", "export", collection.name, str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "NODE_KEY_PATH" in err
