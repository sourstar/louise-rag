"""The pack — one distribution format for a collection's corpus, vectors,
index and proofs (docs/pack-format.md).

    records.py   record shards: JSON Lines in independent zstd frames + index
    slicing.py   how a corpus is split into slices and shards (per plugin)
    manifest.py  the signed manifest
    build.py     `pack build`: a collection's documents -> record shards
    verify.py    `pack verify`: the deterministic local checks

The `packed` plugin (app/plugins/packed) serves a pack as a corpus, so a
collection created over one needs no origin files and no parser.
"""
