"""The `packed` plugin — a pack (docs/pack-format.md) served as a corpus."""
from .source import PackedSource, for_pack

SOURCE = PackedSource()

__all__ = ["SOURCE", "PackedSource", "for_pack"]
