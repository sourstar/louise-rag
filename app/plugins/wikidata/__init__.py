"""The Wikidata plugin (docs/notes/wikidata-build-plan.md): ids, the dump
reader, the pack and label table, the vendored textifier and our formatter,
the chunker `wikidata-text`, the source."""
from .source import WikidataSource

SOURCE = WikidataSource()
