"""tests/ask/test_lookup.py — `louise_lookup(id)` (05 §5.4, §5.2): a fact
by key from the reference source that owns the namespace, every column,
with the source's version; an id with no row is unresolved, never an
empty fact; a malformed id is refused."""
import pytest

from app import refs
from app.ask import lookup


def a_store(tmp_path):
    con = refs.connect(tmp_path / "refs.db") if hasattr(refs, "connect") else None
    if con is None:
        from app.refs import store as refs_store
        con = refs_store.connect(tmp_path / "refs.db")
    schemas = [s.schema() for s in refs.REFS_REGISTRY.values()]
    for schema in schemas:
        refs.ensure_schema(con, schema)
    return con, refs.RefStore(con, schemas)


def test_a_fact_by_key_and_an_unresolved_id(tmp_path):
    con, store = a_store(tmp_path)
    tables = store.tables()
    assert tables, "the registry declares reference tables"
    table = tables[0]
    ns, pk = store.owner_of(table), store.pk_of(table)
    con.execute(f"INSERT INTO {table}({pk}) VALUES(?)", ("4242",))
    con.execute("INSERT INTO ref_meta(source_id, ref_version, synced_at) VALUES(?,?,?)", (ns, 7, 0))
    con.commit()
    out = lookup.lookup(f"{ns}:4242", store=store)
    assert out["unresolved"] == [] and len(out["facts"]) == 1
    (fact,) = out["facts"]
    assert (fact["entity_id"], fact["kind"], fact["source_id"], fact["ref_version"]) == (f"{ns}:4242", table, ns, "7")
    assert fact["fields"][pk] == "4242" or fact["fields"][pk] == 4242
    assert lookup.lookup(f"{ns.upper()}:4242", store=store)["facts"], "the namespace reads lower-case"
    assert lookup.lookup(f"{ns}:nobody", store=store) == {"facts": [], "unresolved": [f"{ns}:nobody"]}
    assert lookup.lookup("nowhere:1", store=store) == {"facts": [], "unresolved": ["nowhere:1"]}


@pytest.mark.parametrize("bad", ["hgnc", ":1", "hgnc:", "", 7])
def test_a_malformed_id_is_refused(bad):
    with pytest.raises(lookup.LookupError_) as e:
        lookup.split(bad)
    assert e.value.exit_code == 2


def test_a_namespace_is_found_through_the_source_that_registers_it():
    """06 §6.5: a namespace is owned by the reference source that registers
    it — `wikidata` by `wikidata-refs`, `urn`/`eli`/`akn` by `normattiva-refs`
    — and a source with no list owns the namespace of its own name."""
    assert lookup.owner_of_namespace("wikidata") == "wikidata-refs"
    assert lookup.owner_of_namespace("urn") == "normattiva-refs"
    assert lookup.owner_of_namespace("eli") == "normattiva-refs"
    assert lookup.owner_of_namespace("hgnc") == "hgnc"
    assert lookup.owner_of_namespace("nowhere") == "nowhere"
