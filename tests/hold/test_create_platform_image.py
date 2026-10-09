"""`hold collection create` pins the platform it runs in (07 §7.3).

`toolchain.platform_image` is hashed into `collection_id` (09 §9.3) and is
"the digest of the platform the command runs in unless the recipe states
it": the recipe's own value, else `--platform-image`, else the digest the
deploy hands the container as LOUISE_PLATFORM_IMAGE. The flag used to
default to `digest(b"")` — the SHA-256 of nothing — and the live gutenberg
Codex was created with exactly that placeholder (found 2026-09-14), an
identity that names no build. With no digest from any of the three, or
with the placeholder, `create` refuses.
"""
import pytest

from app import louise
from app.hold import codex as cx

IMG = "sha256:" + "ab" * 32


def _create(tmp_path, recipe_text: str, *extra) -> int:
    recipe = tmp_path / "recipe.toml"
    recipe.write_text(recipe_text)
    return louise.main(["hold", "collection", "create", "wikipedia",
                        "--recipe", str(recipe), "--root", str(tmp_path), *extra])


@pytest.fixture(autouse=True)
def no_deploy_digest(monkeypatch):
    monkeypatch.delenv("LOUISE_PLATFORM_IMAGE", raising=False)


def test_create_refuses_with_no_platform_image_anywhere(tmp_path, capsys):
    assert _create(tmp_path, "x = 1\n") == 2
    assert "LOUISE_PLATFORM_IMAGE" in capsys.readouterr().err


@pytest.mark.parametrize("where", ["flag", "deploy", "recipe"])
def test_create_refuses_the_empty_bytes_placeholder_from_anywhere(tmp_path, capsys,
                                                                   monkeypatch, where):
    empty = cx.digest(b"")
    extra, recipe = (), "x = 1\n"
    if where == "flag":
        extra = ("--platform-image", empty)
    elif where == "deploy":
        monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", empty)
    else:
        recipe = f'[toolchain]\nplatform_image = "{empty}"\n'
    assert _create(tmp_path, recipe, *extra) == 2
    assert "placeholder" in capsys.readouterr().err


@pytest.mark.parametrize("where", ["flag", "deploy", "recipe"])
def test_a_real_digest_from_the_recipe_the_flag_or_the_deploy_is_what_gets_pinned(
        tmp_path, monkeypatch, where):
    """The digest reaches the Codex builder unchanged, from whichever of the
    three states it. The signing key and the Codex builder are stubbed:
    this is about the value `create` pins, not about writing a drag."""
    from types import SimpleNamespace

    from app.bundles import keys as node_keys
    from app.hold import collection as col
    seen = {}

    def fake_create(root, plugin, recipe, *, platform_image, **kw):
        seen["platform_image"] = platform_image
        return SimpleNamespace(collection_id="sha256:" + "cd" * 32)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda: None)
    monkeypatch.setattr(col, "create_from_recipe", fake_create)
    monkeypatch.setattr(louise, "_write_held_file",
                        lambda root, codex, encoder=None, query_encoder=None, **kw: root)

    extra, recipe = (), '[space]\ndim = 8\ndtype = "f16"\nctx = 512\n'
    if where == "flag":
        extra = ("--platform-image", IMG)
    elif where == "deploy":
        monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", IMG)
    else:
        recipe = f'[toolchain]\nplatform_image = "{IMG}"\n\n' + recipe
    # the encoder is named: a collection names its own, there is no default
    assert _create(tmp_path, recipe, "--encoder", "http://encoder.invalid/upstream/m", *extra) == 0
    assert seen["platform_image"] == IMG
