"""The System page's GPU panel (app/gpu.py, /admin/system/gpu): the web
asks the probe over HTTP and never reads /proc itself."""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, gpu, spaces, web

ARC = gpu.GpuDevice(
    card="card0", pdev="0000:03:00.0", driver="xe", vendor_id="8086", device_id="e223",
    vendor="Intel Corporation", name="Battlemage G31 [Arc Pro B70]",
    vram_total=34242297856, vram_used=25730179072,
    processes=(gpu.GpuProcess(9317, "Qwen3-Embedding-4B-Q4_K_M", "llama-server",
                              "87f3fbd3e86a", 9586248 * 1024, 4103064 * 1024),
               gpu.GpuProcess(371292, "MedCPT-Article-Encoder-f16", "llama-server",
                              "6bedda131c5e", 416 * 1024, 423928 * 1024)))


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("h", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    from app.api import app
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def _probe(monkeypatch, snapshot: gpu.GpuSnapshot) -> list[str]:
    asked: list[str] = []
    monkeypatch.setattr(config, "GPU_PROBE_URL", "http://gpu-probe:8080")
    monkeypatch.setattr(gpu, "_get_json", lambda url: asked.append(url) or gpu.to_json(snapshot))
    return asked


def test_the_panel_shows_the_card_its_memory_and_each_model(client, monkeypatch):
    asked = _probe(monkeypatch, gpu.GpuSnapshot(devices=(ARC,)))
    html = client.get("/admin/system/gpu").text
    assert asked == ["http://gpu-probe:8080/gpu"]
    assert "Battlemage G31 [Arc Pro B70]" in html and "Intel Corporation" in html
    # the node's figures, 2026-09-18: the driver's own used, and free from it
    assert "VRAM 31.89 GiB · used 23.96 GiB" in html and "free 7.93 GiB" in html
    assert "sum of the processes" not in html
    rows = re.findall(r"<tr><td title=\"llama-server\">([^<]+)</td>", html)
    assert rows == ["Qwen3-Embedding-4B-Q4_K_M", "MedCPT-Article-Encoder-f16"]
    assert "9.14 GiB" in html and "28.7%" in html          # Qwen's VRAM, its share
    assert "416 KiB" in html and "414 MiB" in html          # an encoder held in host RAM
    # what the driver counts and no process shown holds
    unattributed = 25730179072 - (9586248 + 416) * 1024
    assert "not held by any process shown" in html and gpu.size(unattributed) in html


def test_without_the_drivers_count_used_is_the_processes_sum(client, monkeypatch):
    from dataclasses import replace
    _probe(monkeypatch, gpu.GpuSnapshot(devices=(replace(ARC, vram_used=None),),
                                        notes=("card0: the driver reports used memory "
                                               "only to CAP_PERFMON",)))
    html = client.get("/admin/system/gpu").text
    assert "sum of the processes" in html and "CAP_PERFMON" in html
    assert "not held by any process shown" not in html
    assert f"used {gpu.size((9586248 + 416) * 1024)}" in html


def test_a_card_whose_memory_is_not_read_says_so(client, monkeypatch):
    from dataclasses import replace
    _probe(monkeypatch, gpu.GpuSnapshot(devices=(replace(
        ARC, driver="i915", vram_total=None, vram_used=None, processes=()),)))
    assert "VRAM not read for the i915 driver" in client.get("/admin/system/gpu").text


def test_unset_says_there_is_no_probe(client, monkeypatch):
    monkeypatch.setattr(config, "GPU_PROBE_URL", "")
    monkeypatch.setattr(gpu, "_get_json", lambda url: pytest.fail("nothing to ask"))
    assert "GPU_PROBE_URL is unset" in client.get("/admin/system/gpu").text


def test_an_unreachable_probe_is_the_panels_state_not_the_pages(client, monkeypatch):
    monkeypatch.setattr(config, "GPU_PROBE_URL", "http://gpu-probe:8080")

    def refused(url):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(gpu, "_get_json", refused)
    r = client.get("/admin/system/gpu")
    assert r.status_code == 200
    assert "unreachable" in r.text and "ConnectError: connection refused" in r.text


def test_the_system_page_loads_the_panel_and_names_the_probe(client, monkeypatch):
    monkeypatch.setattr(config, "GPU_PROBE_URL", "http://gpu-probe:8080")
    page = client.get("/admin/system").text
    assert 'hx-get="/admin/system/gpu"' in page
    assert web._effective_config()["tier1"]["GPU_PROBE_URL"] == "http://gpu-probe:8080"


def test_the_panel_is_an_admin_route(client):
    del client.headers["X-Admin-Token"]
    assert client.get("/admin/system/gpu").status_code == 401


@pytest.mark.parametrize("n,text", [(None, "—"), (512, "512 B"), (416 * 1024, "416 KiB"),
                                    (272148 * 1024, "266 MiB"), (34242297856, "31.89 GiB")])
def test_sizes_are_in_the_drivers_binary_units(n, text):
    assert gpu.size(n) == text


def test_nothing_but_the_probe_imports_the_probe():
    """The reader of the host's processes stays in its own container: no
    module the web or the worker loads may import it."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    importers = [
        str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py")
        if p.name != "gpu_probe.py" and any(
            re.match(r"\s*(from|import)\s", line) and "gpu_probe" in line
            for line in p.read_text(encoding="utf-8").splitlines())]
    assert importers == []


def test_real_host_ram_is_shown_apart_from_mapped_files_and_a_pushed_out_model_is_flagged(
        client, monkeypatch):
    """Operator, 2026-09-20: "separate real RAM occupancy from memory mapped
    RAM". And what the separation showed the same day: another container took
    21 GiB of the card, the driver moved every resident model's buffers to
    host RAM without an error to anyone, and two models died. The panel says
    which host figure is real, and says when a model is no longer on the card."""
    from dataclasses import replace
    qwen, medcpt = ARC.processes
    arc = replace(ARC, processes=(replace(qwen, ram=657 << 20, mapped=2400 << 20), medcpt))
    _probe(monkeypatch, gpu.GpuSnapshot(devices=(arc,)))
    html = client.get("/admin/system/gpu").text
    for heading in ("GPU buffers in host RAM", "process RAM", "mapped file (reclaimable)"):
        assert heading in html
    assert "657 MiB" in html and "2.34 GiB" in html
    assert "never read as occupancy" in html
    # MedCPT: 416 KiB on the card, 414 MiB in host RAM — pushed out; Qwen is not
    assert medcpt.pushed_out and not qwen.pushed_out
    assert html.count("pushed off the card</span>") == 1
    # a row whose split was not read says so, never 0
    assert "<td>—</td>" in html


def test_a_few_mib_of_buffers_in_host_ram_is_not_a_model_pushed_out():
    small = gpu.GpuProcess(1, "probe", "python", "host", vram=0, host=64 << 20)
    assert not small.pushed_out, "every GPU client keeps some buffers in host memory"
    assert gpu.GpuProcess(2, "chat", "llama-server", "rage", vram=476 << 10,
                          host=7403 << 20).pushed_out, "the node, 2026-09-20"


def test_a_probe_one_deploy_ahead_of_the_web_is_still_read():
    snap = gpu.to_json(gpu.GpuSnapshot(devices=(ARC,)))
    snap["devices"][0]["processes"][0]["a_field_from_the_future"] = 1
    back = gpu.from_json(snap)
    assert back.devices[0].processes[0].pid == 9317
    old = gpu.to_json(gpu.GpuSnapshot(devices=(ARC,)))
    for p in old["devices"][0]["processes"]:
        del p["ram"], p["mapped"]                       # a probe one deploy BEHIND
    assert gpu.from_json(old).devices[0].processes[0].ram is None


# ---- whose the memory is, and RAGE's notification (operator, 2026-09-20) ---------------------

RAGE_ID = "1ffc4692b702"
RAGE_SAYS = {
    "clock": {"running": True, "pinned": False, "rest_mhz": 400, "max_mhz": 2800, "error": None},
    "container": RAGE_ID,
    "memory": {"xpu": {"read": True, "why_not": None, "total_mib": 32656, "used_mib": 28458,
                       "own_mib": 15900, "foreign_mib": 12558, "free_mib": 4198,
                       "reserve_mib": 2048, "declared_held_mib": 25000,
                       "own_in_host_ram_mib": 13800, "squeezed": False,
                       "pushed_out": ["qwen3-embedding-4b"],
                       "unloaded_under_pressure": ["bge-reranker"]}},
    "events": [{"time": 1_700_000_000, "kind": "unloaded-under-pressure", "model": "bge-reranker",
                "device": "xpu", "count": 1,
                "detail": "idle for 412 s and unloaded: processes outside RAGE hold 12558 MiB"},
               {"time": 1_700_000_100, "kind": "refused", "model": "madlad", "device": "xpu",
                "count": 7, "detail": "madlad needs 6800 MiB of device 'xpu'; 1100 MiB are free "
                                      "of 32600 (17000 held, 12558 held by processes outside "
                                      "RAGE, 2048 reserved)"}]}


def _rage(monkeypatch, snapshot: gpu.GpuSnapshot, says: dict | None = RAGE_SAYS):
    from dataclasses import replace  # noqa: F401
    monkeypatch.setattr(config, "GPU_PROBE_URL", "http://gpu-probe:8080")
    monkeypatch.setattr(config, "RAGE_URL", "http://rage:8080")

    def get(url):
        if url.endswith("/louise/gpu"):
            if says is None:
                raise OSError("connection refused")
            return says
        return gpu.to_json(snapshot)
    monkeypatch.setattr(gpu, "_get_json", get)


def test_the_bar_says_what_is_rages_and_what_is_not(client, monkeypatch):
    from dataclasses import replace
    qwen, medcpt = ARC.processes
    stranger = gpu.GpuProcess(119728, "tools.bakeoff_local", "python", "2b06229d3262",
                              12524 << 20, 4408 << 20)
    arc = replace(ARC, vram_used=None, processes=(
        replace(qwen, container=RAGE_ID), replace(medcpt, container=RAGE_ID), stranger))
    shares = arc.shares(RAGE_ID)
    assert round(shares["rage"], 1) == 28.7 and round(shares["others"], 1) == 38.4
    assert round(sum(shares.values()), 3) == 100.0
    assert arc.shares(None)["rage"] == 0.0, "RAGE not identified: nothing is claimed as its own"

    _rage(monkeypatch, gpu.GpuSnapshot(devices=(arc,)))
    html = client.get("/admin/system/gpu").text
    assert 'class="rage" style="width:28.7%"' in html and 'class="others" style="width:38.4%"' in html
    assert html.count("</span>RAGE</td>") == 2 and html.count("</span>other</td>") == 1
    assert "outside RAGE 12558" in html and "could not load" in html and "×7" in html
    assert "unloaded, idle" in html


def test_louise_is_told_when_rage_cannot_fit_a_model_and_is_silent_otherwise(client, monkeypatch):
    import time as time_mod
    fresh = {**RAGE_SAYS, "events": [{**e, "time": time_mod.time() - 30}
                                     for e in RAGE_SAYS["events"]]}
    _rage(monkeypatch, gpu.GpuSnapshot(devices=(ARC,)), fresh)
    html = client.get("/admin/system/rage-alerts").text
    assert "RAGE could not load madlad (7 times)" in html
    assert "12558 held by processes outside RAGE" in html
    assert "pushed off the card into host RAM: qwen3-embedding-4b" in html
    assert "unloaded under memory pressure" in html and "bge-reranker" in html

    # an old refusal is history, and a healthy card says NOTHING
    quiet = {**RAGE_SAYS, "memory": {"xpu": {**RAGE_SAYS["memory"]["xpu"], "pushed_out": [],
                                             "unloaded_under_pressure": []}}}
    _rage(monkeypatch, gpu.GpuSnapshot(devices=(ARC,)), quiet)
    assert client.get("/admin/system/rage-alerts").text.strip() == ""
    # RAGE away: the strip stays empty, the panel says so
    _rage(monkeypatch, gpu.GpuSnapshot(devices=(ARC,)), None)
    assert client.get("/admin/system/rage-alerts").text.strip() == ""
    assert "RAGE does not answer" in client.get("/admin/system/gpu").text


def test_the_strip_is_on_the_pages_an_operator_watches():
    templates = Path(web.__file__).parent / "templates"
    for page in ("library.html", "system.html"):
        assert 'hx-get="/admin/system/rage-alerts"' in (templates / page).read_text("utf-8")


def test_the_worker_names_rages_reason_when_rage_refused_the_model():
    import httpx
    from app import worker
    request = httpx.Request("POST", "http://rage/upstream/qwen3-embedding-4b/v1/embeddings")
    refused = httpx.Response(503, request=request, json={
        "error": "qwen3-embedding-4b needs 9000 MiB of device 'xpu'; 1100 MiB are free",
        "admission": {"device": "xpu", "foreign_mib": 12558}})
    busy = httpx.Response(503, request=request, text="loading")
    try:
        try:
            refused.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError("embed server HTTP 503") from e
    except RuntimeError as wrapped:
        assert worker.transient_failure(wrapped), "still a deferral, never a document's error"
        assert "needs 9000 MiB" in worker.rage_refusal(wrapped)
    try:
        busy.raise_for_status()
    except httpx.HTTPStatusError as e:
        assert worker.rage_refusal(e) is None, "busy is not a refusal"
    assert worker.rage_refusal(ValueError("x")) is None
