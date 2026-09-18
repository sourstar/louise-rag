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
