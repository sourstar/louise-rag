"""`hold collection create` names the platform it runs in, or refuses.

`toolchain.platform_image` is hashed into `collection_id` (09 §9.3): it is
the build that parses, cleans, chunks and checks this collection's records.
The flag defaulted to `digest(b"")` — the SHA-256 of nothing — and the live
gutenberg Codex was created with exactly that placeholder (found
2026-09-14), an identity that names no build at all. The recipe path now
refuses a missing or empty-bytes digest before it reads anything; a whole
Codex file carries its own toolchain and is unaffected.
"""
from app import louise
from app.hold import codex as cx


def _create(tmp_path, *extra):
    return louise.main(["hold", "collection", "create", "wikipedia",
                        "--recipe", str(tmp_path / "recipe.toml"),
                        "--root", str(tmp_path), *extra])


def test_create_refuses_without_a_platform_image(tmp_path, capsys):
    assert _create(tmp_path) == 2
    assert "--platform-image" in capsys.readouterr().err


def test_create_refuses_the_empty_bytes_placeholder(tmp_path, capsys):
    assert _create(tmp_path, "--platform-image", cx.digest(b"")) == 2
    assert "placeholder" in capsys.readouterr().err
