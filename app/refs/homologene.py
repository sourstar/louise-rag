"""HomoloGene orthology — §9's multi-species extension, adjustment 2:
"orthology is a new link type" feeding expand() ("human gene → mouse
ortholog → knockout phenotype"). NCBI's HomoloGene (frozen build 68, a
~16 MB TSV, public domain) groups orthologous genes across 21 species by
HomoloGene group id; any two rows sharing a group id are orthologs.

Species-qualified keys per §9: the table keys on (group, taxid, symbol) so
TP53/Trp53 never collide; the symbol link into hgnc_gene resolves the human
member, and expand() walks the group for the rest.
"""
import csv

from ..plugins.base import RefColumn, RefLink, RefRecordBatch, RefSchema, RefSyncState, RefTable

TABLE = RefTable(
    name="homologene",
    columns=(
        RefColumn("key", "TEXT"),            # "<group>:<taxid>:<symbol>"
        RefColumn("group_id", "INTEGER"),
        RefColumn("taxid", "INTEGER"),
        RefColumn("gene_id", "TEXT"),
        RefColumn("symbol", "TEXT"),
    ),
    primary_key="key",
)

LINKS = (
    RefLink(name="ortholog_to_gene", from_table="homologene",
            from_column="symbol", to_table="hgnc_gene", to_column="symbol"),
)


class HomologeneSource:
    source_id = "homologene"
    ref_version = 1

    def schema(self) -> RefSchema:
        return RefSchema(source_id=self.source_id, tables=(TABLE,), links=LINKS)

    def sync(self, state: RefSyncState):
        path = state.ref_root / "homologene.tsv"
        if not path.is_file():
            return
        with open(path, newline="", encoding="utf-8") as f:
            rows = tuple(dict(r) for r in csv.DictReader(f, delimiter="\t"))
        if rows:
            yield RefRecordBatch(table=TABLE.name, rows=rows)
