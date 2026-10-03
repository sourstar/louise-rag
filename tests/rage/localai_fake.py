"""A LocalAI with nothing real under it, for the front over LocalAI (migration
step 15d): a directory for /proc, a models directory, and the HTTP routes the
front calls, behaving as LocalAI does at 58830f7 with deploy/localai/patches
applied.

What it does that matters to a run:
- every load starts a NEW backend process (`spawnGRPCModel`), with
  `--addr 127.0.0.1:<port>` and the model's `env:` on top of its own;
- it completes the model's YAML before the load (§10): an unset
  `context_size`, `threads` or `parameters.batch` comes from the
  application's settings, which a test can change the way LocalAI's UI does;
- `GET /system` lists each loaded model's process, `size_vram` and the
  options it sent (`load_options`, with a float written as Go writes one)
  and their hash.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from app.rage.front_localai import MARKER

PCI = "0000:03:00.0"
BACKEND_ARGV = "/backends/llama-cpp/llama-cpp-fallback"


def hand_written(models_dir: Path, name: str, config: dict[str, Any]) -> Path:
    """The operator's own file: no marker."""
    path = models_dir / f"{name}.yaml"
    path.write_text(json.dumps(dict(config, name=name)), encoding="utf-8")
    return path


def pinned(model_file: str, **over: Any) -> dict[str, Any]:
    """A YAML that pins every option of rage-over-localai §10."""
    return {"backend": "llama-cpp", "context_size": 8192, "threads": 8, "gpu_layers": 99,
            "parameters": {"model": model_file, "batch": 512},
            "options": ["parallel:1"], "embeddings": True, **over}


class FakeLocalAI:
    def __init__(self, root: Path) -> None:
        self.proc = root / "proc"
        self.proc.mkdir(parents=True)
        self.btime = int(time.time()) - 10_000
        (self.proc / "stat").write_text(f"cpu  1 2 3\nbtime {self.btime}\n")
        self.models_dir = root / "models"
        self.models_dir.mkdir()
        (self.models_dir / "bge-m3.gguf").write_bytes(b"GGUF bge-m3 weights")
        (self.models_dir / "reranker.gguf").write_bytes(b"GGUF reranker weights")
        self._pids = itertools.count(300)
        self._ports = itertools.count(40001)
        #: LocalAI's application settings, as its settings page changes them
        self.settings: dict[str, Any] = {"threads": 8, "context_size": 4096, "batch": 512}
        self.configs: dict[str, dict[str, Any]] = {}
        self.loaded: dict[str, int] = {}            # model -> pid
        self.options: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.on_card_mib = 1400                     # what a loaded model holds of the card
        self.pdev: str | None = PCI
        self.during_request: Callable[[], None] | None = None
        self.reload_configs()

    # ---- the fake kernel ------------------------------------------------------------

    def spawn(self, argv: list[str], env: dict[str, str], *, card_mib: int) -> int:
        pid = next(self._pids)
        d = self.proc / str(pid)
        (d / "fdinfo").mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "environ").write_bytes(
            b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
        ticks = int((time.time() - self.btime) * os.sysconf("SC_CLK_TCK")) - 1
        fields = ["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 5
        (d / "stat").write_text(f"{pid} (llama-cpp-fallb) " + " ".join(fields) + "\n")
        if card_mib:
            pdev = f"drm-pdev:\t{self.pdev}\n" if self.pdev else ""
            for fd in ("5", "6"):                    # one client through two descriptors
                (d / "fdinfo" / fd).write_text(f"drm-driver:\txe\n{pdev}drm-client-id:\t7\n"
                                               f"drm-resident-vram0:\t{card_mib} MiB\n")
        return pid

    def kill(self, pid: int) -> None:
        for f in sorted((self.proc / str(pid)).rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        (self.proc / str(pid)).rmdir()

    # ---- what LocalAI does ------------------------------------------------------------

    def reload_configs(self) -> None:
        """`POST /models/reload`: every YAML in the models directory, again —
        and a config whose file is gone is forgotten."""
        self.configs = {}
        for path in sorted(self.models_dir.glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            if text.startswith(MARKER):
                text = text.split("\n", 1)[1]
            config = json.loads(text)
            self.configs[config["name"]] = config

    def materialise(self, name: str) -> dict[str, Any]:
        """The options the backend is sent: the YAML completed with the
        application's settings wherever it is silent (§10)."""
        c = self.configs[name]
        params = c.get("parameters") or {}
        options = {"Model": params["model"],
                   "ModelFile": f"{self.models_dir}/{params['model']}",
                   "ModelPath": str(self.models_dir),
                   "ContextSize": c.get("context_size", self.settings["context_size"]),
                   "Threads": c.get("threads", self.settings["threads"]),
                   "NBatch": params.get("batch", self.settings["batch"]),
                   "NGPULayers": c.get("gpu_layers", 99),
                   "Embeddings": bool(c.get("embeddings")),
                   "RopeFreqBase": 10000.5}
        if c.get("options"):
            options["Options"] = list(c["options"])
        if c.get("env"):
            options["EnvVars"] = dict(c["env"])
        for key in ("MMProj", "DraftModel"):
            if key.lower() in c:
                options[key] = c[key.lower()]
        return options

    def load(self, name: str) -> int:
        if name in self.loaded:
            return self.loaded[name]
        opts = self.materialise(name)
        env = {"PATH": "/usr/bin", "HOSTNAME": "rage-1", **opts.get("EnvVars", {})}
        argv = [BACKEND_ARGV, "--addr", f"127.0.0.1:{next(self._ports)}"]
        pid = self.spawn(argv, env, card_mib=self.on_card_mib if opts["NGPULayers"] else 0)
        self.loaded[name], self.options[name] = pid, opts
        return pid

    def shutdown(self, name: str) -> bool:
        pid = self.loaded.pop(name, None)
        if pid is None:
            return False
        self.kill(pid)
        return True

    def replace(self, name: str) -> None:
        """LocalAI evicts the model and loads it again: another process."""
        self.shutdown(name)
        self.load(name)

    # ---- the HTTP routes ----------------------------------------------------------------

    def _system(self) -> str:
        rows = []
        for name, pid in sorted(self.loaded.items()):
            opts = self.options[name]
            digest = hashlib.sha256(json.dumps(opts, sort_keys=True).encode()).hexdigest()
            rows.append({"id": name, "backend": self.configs.get(name, {}).get("backend"),
                         "process": {"pid": pid, "rss_bytes": 1, "memory_percent": 0.5,
                                     "started_at": "2026-10-03T08:00:00.123456789Z"},
                         "size_vram": self.on_card_mib * (1 << 20)
                         if opts["NGPULayers"] else 0,
                         "load_options": opts, "load_options_hash": digest})
        return json.dumps({"backends": ["llama-cpp"], "loaded_models": rows})

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            body = json.loads(request.content) if request.content else None
            self.calls.append((request.method, path, body))
            if path == "/system":
                return httpx.Response(200, text=self._system(),
                                      headers={"content-type": "application/json"})
            if path == "/version":
                return httpx.Response(200, json={"version": "v3.9.0"})
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": n} for n in sorted(self.configs)]})
            if path == "/models/reload":
                self.reload_configs()
                return httpx.Response(200, json={"success": True})
            if path == "/backend/load":
                if body["model"] not in self.configs:
                    return httpx.Response(500, json={"message": "no such model"})
                self.load(body["model"])
                return httpx.Response(200, json={"loaded": [body["model"]], "message": "ok"})
            if path == "/backend/shutdown":
                if not self.shutdown(body["model"]):
                    return httpx.Response(404, json={"error": "model not loaded"})
                return httpx.Response(200, json={})
            if path in ("/v1/embeddings", "/v1/rerank"):
                name = body["model"]
                if name not in self.configs:
                    return httpx.Response(404, json={"error": f"model {name} not found"})
                self.load(name)
                if self.during_request is not None:
                    self.during_request()
                return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5]}],
                                                 "model": name})
            return httpx.Response(404, json={"error": f"no route {path}"})
        return httpx.MockTransport(handle)

    def admin_calls(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[1] in ("/backend/load", "/backend/shutdown",
                                                 "/models/reload")]
