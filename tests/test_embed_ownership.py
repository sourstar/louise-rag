"""A CPU encoder must never be able to starve the GPU.

"GPU-serial across collections" exists because one GPU cannot serve two
collections at once. It says nothing about CPU encoders, which are separate
containers on separate cores. Making the main loop the single embed owner
for everyone inverted that: _embed_parallel took a batch of four yeast
chromosomes, blocked the loop for a quarter of an hour, and because
collections are visited in sort order `genome-sacCer3` sits ahead of
`gutenberg` — so the GPU idled at 0% CPU waiting for a CPU encoder.
"""
import pytest

from app import collections as collections_mod, worker


def _collection(tmp_path, name, recipe):
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / f"{name}.db",
        fts_dir=tmp_path / f"{name}-fts", lance_dir=tmp_path / f"{name}-lance",
        embed=recipe)


GPU = collections_mod.EmbedRecipe(url="http://gpu", api="openai", model="m",
                                   dtype="f16", ctx=8192, concurrency=1)
CPU = collections_mod.EmbedRecipe(url="http://cpu", api="openai", model="m",
                                   dtype="f16", ctx=1024, concurrency=2)
CPU_EXPLICIT = collections_mod.EmbedRecipe(url="http://cpu", api="openai",
                                            model="m", dtype="f16", ctx=1024,
                                            concurrency=1, device="cpu")


def test_cpu_collections_own_their_embedding(tmp_path):
    assert worker.owns_own_embed(_collection(tmp_path, "dna", CPU)) is True
    assert worker.owns_own_embed(_collection(tmp_path, "tei", CPU_EXPLICIT)) is True


def test_gpu_collections_stay_with_the_main_loop(tmp_path):
    """The doctrine's actual subject: one GPU, one owner, strictly serial."""
    assert worker.owns_own_embed(_collection(tmp_path, "books", GPU)) is False


def test_the_split_follows_the_recipe_not_the_plugin(tmp_path, isolated_config):
    """Same plugin, different endpoint: what matters is which processor the
    collection spends, which is exactly what recipe.spends reports."""
    gpu = _collection(tmp_path, "a", GPU)
    cpu = _collection(tmp_path, "b", CPU)
    assert gpu.plugin == cpu.plugin
    assert worker.owns_own_embed(gpu) != worker.owns_own_embed(cpu)


def test_a_collection_without_a_recipe_follows_the_node_default(tmp_path,
                                                                 isolated_config,
                                                                 monkeypatch):
    """No [embed] block means the env recipe, which on this node is the GPU
    encoder — so it must stay on the main loop."""
    from app import config
    monkeypatch.setattr(config, "EMBED_CONCURRENCY", 1)
    plain = _collection(tmp_path, "plain", None)
    assert worker.owns_own_embed(plain) is False


def test_the_main_loop_skips_self_owned_collections(tmp_path, isolated_config,
                                                     monkeypatch):
    """The starvation half of the bug: even with the work moved onto its own
    thread, the main loop must stop visiting it — otherwise it blocks there
    exactly as before, and two threads embed the same collection."""
    import inspect
    src = inspect.getsource(worker.run_forever)
    assert "owns_own_embed(collection)" in src, \
        "the main loop must consult the ownership rule"
    # and the skip has to come BEFORE the round budget, or the loop still
    # blocks inside _run_one_step for the length of a batch
    skip = src.index("owns_own_embed(collection)")
    budget = src.index("round_deadline = time.time()")
    assert skip < budget, "the skip must precede the work, not follow it"
