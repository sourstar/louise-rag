"""The pack — one distribution format for a collection's corpus, vectors,
index and proofs (docs/notes/pack-format.md).

    records.py   record shards: JSON Lines in independent zstd frames + index
    slicing.py   how a corpus is split into slices and shards (per plugin)
    manifest.py  the signed manifest
    build.py     `pack build`: a collection's documents -> record shards
    verify.py    `pack verify`: the deterministic local checks

The `packed` plugin, which served a pack as a corpus, and `pack adopt` are
retired (2026-09-14; removed from the tree 2026-11-01, attic/ at c572c3b).
"""
