"""The System page's GPU panel by model (app/gpu_models.py): every model
loaded in LocalAI with its VRAM, marked Louise's or the operator's, and what
the card holds outside them (operator, 2026-10-12: "either it can list all
the louise/others usage or it is pointless")."""
from types import SimpleNamespace as NS

from app import gpu_models as gm

GIB = 1 << 30

COLLECTIONS = [
    NS(name="gutenberg", embed=NS(url="http://nas:30286", model="vega-qwen3", query_url=None, query_model="vega-qwen3"),
       reranker=NS(url="http://nas:30286/v1/rerank", model="louise-rerank-bge")),
    NS(name="pubmed", embed=NS(url="http://nas:30286", model="vega-article", query_url="http://nas:30286",
                               query_model="vega-query"),
       reranker=NS(url="http://nas:30286/v1/rerank", model="louise-rerank-medcpt")),
]

SYSTEM = {"loaded_models": [
    {"id": "vega-qwen3", "backend": "vega-llama-cpp", "size_vram": 9 * GIB},
    {"id": "vega-article", "backend": "vega-louise-medcpt-article-ov", "size_vram": GIB // 2},
    {"id": "vega-query", "backend": "vega-llama-cpp", "size_vram": GIB // 2},
    {"id": "louise-rerank-bge", "backend": "intel-sycl-f16-llama-cpp", "size_vram": GIB},
    {"id": "flux-dev", "backend": "stablediffusion-ggml", "size_vram": 6 * GIB},
]}


def test_louise_uses_name_the_collection_and_the_role():
    uses = gm.louise_uses(COLLECTIONS, chat_model="louise-chat", translate_model="")
    assert uses["vega-qwen3"] == ["gutenberg: embedding and queries"]
    assert uses["vega-article"] == ["pubmed: embedding"] and uses["vega-query"] == ["pubmed: queries"]
    assert uses["louise-rerank-medcpt"] == ["pubmed: reranking"]
    assert uses["louise-chat"] == ["chat"] and "" not in uses


def test_one_origin_per_host_whatever_the_paths():
    assert gm.origins(COLLECTIONS, ("http://nas:30286/v1/chat", "", "http://other:8080/x")) == \
        ["http://nas:30286", "http://other:8080"]


def test_the_panel_marks_every_model_and_sums_the_two_sides():
    asked = []

    def get_json(url):
        asked.append(url)
        if url.startswith("http://other"):
            return {"detail": "not a LocalAI"}       # answers, but is no LocalAI: skipped
        return SYSTEM
    p = gm.panel(COLLECTIONS, extra_urls=("http://other:8080",), get_json=get_json)
    assert asked == ["http://nas:30286/system", "http://other:8080/system"]
    assert p["asked"] == ["http://nas:30286"] and p["failed"] == []
    assert [r.model for r in p["rows"]][:2] == ["vega-qwen3", "flux-dev"], "largest first"
    flux = next(r for r in p["rows"] if r.model == "flux-dev")
    assert not flux.louise and flux.uses == ()
    assert p["louise"] == 11 * GIB and p["other"] == 6 * GIB
    b = gm.bar(p, total=32 * GIB, used=18 * GIB)
    assert b["outside_bytes"] == GIB, "what the card counts and no model holds"
    assert round(b["louise"] + b["other"] + b["outside"] + b["free"], 6) == 100.0


def test_a_localai_that_does_not_answer_is_named_and_no_bar_is_drawn():
    def get_json(url):
        raise TimeoutError("down")
    p = gm.panel(COLLECTIONS, get_json=get_json)
    assert p["rows"] == [] and p["failed"] == ["http://nas:30286: TimeoutError"]
    assert gm.bar(p, total=32 * GIB, used=18 * GIB) is None


def test_two_addresses_of_one_localai_count_its_models_once():
    system = {"loaded_models": [{"id": "vega-qwen3", "backend": "b", "size_vram": GIB,
                                 "process": {"pid": 91, "started_at": "2026-10-11T09:25:28Z"}}]}
    p = gm.panel(COLLECTIONS, extra_urls=("http://192.168.178.10:30286",), get_json=lambda u: system)
    assert len(p["asked"]) == 2 and len(p["rows"]) == 1 and p["louise"] == GIB


def test_the_card_its_readings_and_the_governor_come_before_its_models():
    """The operator (2026-10-30): on the System tab the GPU's own figures
    come first, the list of the models it holds after them."""
    from dataclasses import replace
    from app import gpu, web
    p = gm.panel(COLLECTIONS, get_json=lambda url: SYSTEM)
    dev = gpu.GpuDevice(card="card0", pdev="0000:03:00.0", driver="xe", vendor_id="8086",
                        device_id="e223", vendor="Intel", name="Arc Pro B70",
                        vram_total=32 * GIB, vram_used=18 * GIB,
                        sensors=gpu.GpuSensors(fans=(1910,), temps=(("pkg", 73.0),), power_w=181.4))
    body = web.TEMPLATES.get_template("_fragments/gpu.html").render(
        state="ok", url="x", devices=(dev,), notes=(), models=p, model_bar=gm.bar,
        governor={"state": "running", "pinned": True, "rest_mhz": 400, "max_mhz": 2800})
    card, vram, fan = body.index("Arc Pro B70"), body.index("VRAM 32"), body.index("fan 1910 RPM")
    clock, models = body.index("clock held at 2800 MHz"), body.index("<th>model</th><th>used by</th>")
    assert card < vram < fan < clock < models, "the card's figures, then its models"
    assert body.count("GPU governor") == 1, "the governor once, in the card"
    alone = web.TEMPLATES.get_template("_fragments/gpu.html").render(
        state="ok", url="x", devices=(replace(dev, vram_total=None, vram_used=None),), notes=(),
        governor={"state": "absent"})
    assert alone.count("GPU governor not running") == 1, "a card that reads no VRAM still shows it"
