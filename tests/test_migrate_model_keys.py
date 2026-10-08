"""tools/migrate_model_keys.py — a collection file's `model` becomes 07 §7.8's
`embed_model`/`query_model` (and `[reranker]`'s `rerank_model`), in place,
comments and layout kept; a file whose values cannot be determined is
refused and left byte for byte (operator, 2026-10-08)."""
import tomllib

import pytest

from app import collections as collections_mod
from tools import migrate_model_keys as mig

CODEX = 'codex = "sha256:' + "ab" * 32 + '"\n'


def test_model_becomes_both_keys_and_every_comment_stays():
    before = (CODEX +
              "# pubmed, MedCPT pair\n"
              'embed = "on-demand"   # what the node spends\n'
              "\n"
              "[encoder]   # where this node reaches it\n"
              'url = "http://nas:30437/upstream/medcpt-article"\n'
              'query_url = "http://nas:30437/upstream/medcpt-query"  # the query side\n'
              'model = "medcpt-article"   # the article encoder\n'
              'api = "openai"\n'
              "\n"
              "[reranker]\n"
              'url = "http://nas:30437/upstream/medcpt-rerank/v1/rerank"\n'
              'api = "jina"\n'
              "# trailing comment\n")
    after = mig.migrate_text(before)
    assert after == (CODEX +
                     "# pubmed, MedCPT pair\n"
                     'embed = "on-demand"   # what the node spends\n'
                     "\n"
                     "[encoder]   # where this node reaches it\n"
                     'url = "http://nas:30437/upstream/medcpt-article"\n'
                     'query_url = "http://nas:30437/upstream/medcpt-query"  # the query side\n'
                     'embed_model = "medcpt-article"   # the article encoder\n'
                     'query_model = "medcpt-query"\n'
                     'api = "openai"\n'
                     "\n"
                     "[reranker]\n"
                     'url = "http://nas:30437/upstream/medcpt-rerank/v1/rerank"\n'
                     'api = "jina"\n'
                     'rerank_model = "medcpt-rerank"\n'
                     "# trailing comment\n")
    # and the node loads what it wrote
    raw = tomllib.loads(after)
    r = collections_mod.parse_recipe(raw["encoder"], where="t")
    assert (r.model, r.query_model) == ("medcpt-article", "medcpt-query")
    assert collections_mod.parse_reranker(raw["reranker"], where="t").model == "medcpt-rerank"
    assert mig.migrate_text(after) == after, "a migrated file is left alone"


def test_symmetric_localai_query_model_is_the_model():
    before = CODEX + '[encoder]\nurl = "http://localai:8080"\napi = "vega"\nmodel = "vega-bge-m3"\n'
    after = mig.migrate_text(before)
    assert after.endswith('embed_model = "vega-bge-m3"\nquery_model = "vega-bge-m3"\n')
    assert "\nmodel" not in after


def test_no_model_line_takes_both_from_the_urls():
    before = CODEX + '\n[encoder]\nurl = "http://rage:1/upstream/bge-m3"\n\n# end\n'
    assert mig.migrate_text(before) == (
        CODEX + '\n[encoder]\nurl = "http://rage:1/upstream/bge-m3"\n'
        'embed_model = "bge-m3"\nquery_model = "bge-m3"\n\n# end\n')


def test_reranker_model_is_renamed():
    before = CODEX + '[reranker]\nurl = "http://gpu:30447/v1/rerank"\nmodel = "bge-reranker-v2-m3"\n'
    assert mig.migrate_text(before) == (
        CODEX + '[reranker]\nurl = "http://gpu:30447/v1/rerank"\nrerank_model = "bge-reranker-v2-m3"\n')


def test_crlf_and_no_final_newline_are_kept():
    before = (CODEX + '[encoder]\nurl = "http://rage:1/upstream/m"\nmodel = "m"').replace("\n", "\r\n")
    after = mig.migrate_text(before)
    assert after == (CODEX + '[encoder]\nurl = "http://rage:1/upstream/m"\n'
                     'embed_model = "m"\nquery_model = "m"').replace("\n", "\r\n")


@pytest.mark.parametrize("text,why", [
    # no model, and a URL that names none
    (CODEX + '[encoder]\nurl = "http://localai:8080"\n', "embed_model cannot be determined"),
    # a separate query endpoint naming no model: X would be the document model
    (CODEX + '[encoder]\nurl = "http://a:1"\nquery_url = "http://q:2"\nmodel = "a"\n',
     "query_model cannot be determined"),
    # a stated model the RAGE URL contradicts: the node would refuse the result
    (CODEX + '[encoder]\nurl = "http://rage:1/upstream/bge-m3"\nmodel = "BGE-M3"\n', "names 'bge-m3'"),
    (CODEX + '[reranker]\nurl = "http://gpu:30447/v1/rerank"\n', "rerank_model cannot be determined"),
    (CODEX + "[encoder\n", "not a TOML file"),
])
def test_what_cannot_be_determined_is_refused_and_nothing_written(tmp_path, capsys, text, why):
    p = tmp_path / "c.toml"
    p.write_bytes(text.encode())
    assert mig.main([str(p)]) == 1
    assert why in capsys.readouterr().err
    assert p.read_bytes() == text.encode(), "byte for byte as it was"


def test_a_directory_each_file_on_its_own(tmp_path, capsys):
    good = tmp_path / "good.toml"
    bad = tmp_path / "bad.toml"
    done = tmp_path / "done.toml"
    good.write_text(CODEX + '[encoder]\nurl = "http://rage:1/upstream/m"\nmodel = "m"\n', encoding="utf-8")
    bad.write_text(CODEX + '[encoder]\nurl = "http://x:1"\n', encoding="utf-8")
    done.write_text(CODEX + '[encoder]\nurl = "http://x:1"\nembed_model = "m"\nquery_model = "m"\n',
                    encoding="utf-8")
    before = done.read_bytes()
    assert mig.main(["--dry-run", str(tmp_path)]) == 1
    assert "model = " in good.read_text(encoding="utf-8") and "embed_model" not in good.read_text(
        encoding="utf-8"), "a dry run writes nothing"
    assert mig.main([str(tmp_path)]) == 1, "one refusal is exit 1"
    out = capsys.readouterr().out
    assert f"migrated: {good}" in out and f"unchanged: {done}" in out
    assert 'embed_model = "m"\nquery_model = "m"\n' in good.read_text(encoding="utf-8")
    assert done.read_bytes() == before
    assert list(tmp_path.glob("*.migrating")) == []
