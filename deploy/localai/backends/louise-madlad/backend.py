"""MADLAD-400-3B-MT as a LocalAI backend: `Predict`, on torch XPU (migration
step 15e-2). The model is deploy/madlad-translate/server.py, copied beside this
file as `madlad.py`; why it is a Python server is there (neither Ollama nor
llama-server runs its encoder).

THE BATCH TRAVELS IN ONE PROMPT (operator, 2026-10-03). LocalAI's
/v1/completions calls `Predict` once per prompt, one after another, and MADLAD
is on the node because it translates a whole batch in one pass: a query into
51 languages in 1.5 s, where one pass per language would cost one each. So the
prompt is a JSON request and the completion text is its JSON answer — a format
only Louise's client speaks (app/translate.py):

    POST /v1/completions {"prompt": "{\"items\": [{\"text\": str, \"to\": code}, ...]}"}
      -> choices[0].text = {"translations": [str | null, ...], "ms": int, "peak_mb": int | null}
    POST /v1/completions {"prompt": "{\"languages\": true}"}
      -> choices[0].text = {"languages": [code, ...]}

`null` is an item whose target the model does not know — never the input
handed back as if it were a translation. LocalAI passes both texts through
untouched while the model's YAML sets no template and none of `echo`,
`cutstrings`, `extract_regex`, `trimspace` or `trimsuffix`
(`core/backend/llm.go`, Finetune); tests/rage/test_localai_deploy.py holds the
YAML to that.

Parking (rage.md §21) is not here: it was the llama-swap placement's, retired at
15g with it (LocalAI unloads instead; rage-over-localai §7).

The load call:
    ModelFile     the snapshot directory
    NBatch        segments per pass (the server's BATCH, 32: memory is the
                  budget on a shared card — the measurements are in server.py)
    options       device:xpu|cpu (default xpu), dtype:bfloat16|float16|float32
                  (bfloat16), max_seg_tokens (96), beams (1), max_items (512)
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

import louise_backend as lb
import madlad

DTYPES = ("bfloat16", "float16", "float32")


def dtype(value: str) -> str:
    value = value.strip()
    if value not in DTYPES:
        raise ValueError(f"one of {', '.join(DTYPES)}")
    return value


KNOWN = {"device": lb.device, "dtype": dtype, "max_seg_tokens": lb.positive,
         "beams": lb.positive, "max_items": lb.positive}
DEFAULTS = {"device": "xpu", "dtype": "bfloat16", "max_seg_tokens": 96, "beams": 1,
            "max_items": 512}
#: madlad.handle's refusals, as gRPC says them; LocalAI answers them as errors
#: and the client falls back to the chat model, saying so
REFUSALS = {422: "INVALID_ARGUMENT", 413: "RESOURCE_EXHAUSTED"}


class Servicer:
    def __init__(self, pb: Any, status: Any, *, load=madlad.load,
                 settle=None) -> None:
        self.pb, self.status = pb, status
        self._load = load
        #: what the server's main does once the model is warm: the host's
        #: freed pages and the weights' page cache handed back
        self._settle = settle or _settle
        self.translator: madlad.Translator | None = None
        self.languages: list[str] = []
        self.max_items = DEFAULTS["max_items"]

    def Health(self, request, context):  # noqa: N802 — gRPC's names
        return self.pb.Reply(message=b"OK")

    def LoadModel(self, request, context):  # noqa: N802
        try:
            opts = lb.options(request.Options, KNOWN, DEFAULTS)
            path = lb.weights(request.ModelFile)
            if request.NBatch <= 0:
                raise lb.LoadError("batch (segments per pass) is set in the model's YAML")
            t0 = time.monotonic()
            count, generate, known, languages, release = self._load(
                str(path), opts["device"], opts["dtype"], beams=opts["beams"])
            translator = madlad.Translator(count, generate, known, batch=request.NBatch,
                                           max_seg_tokens=opts["max_seg_tokens"],
                                           release=release)
            translator.translate([("the first pass compiles its kernels", "de")])
            self._settle(str(path))
        except Exception as e:                          # noqa: BLE001 — said to LocalAI
            return lb.refused(self.pb, e)
        self.translator, self.languages = translator, list(languages)
        self.max_items = opts["max_items"]
        return lb.loaded(self.pb, f"{path.name} on {opts['device']}: loaded and warm in "
                                  f"{time.monotonic() - t0:.1f}s, {len(languages)} "
                                  f"target languages")

    def Predict(self, request, context):  # noqa: N802
        if self.translator is None:
            context.abort(self.status.FAILED_PRECONDITION, "no model is loaded")
        try:
            body = json.loads(request.Prompt)
        except ValueError:
            context.abort(self.status.INVALID_ARGUMENT,
                          'the prompt is a JSON request: {"items": [...]} or '
                          '{"languages": true}')
        if body == {"languages": True}:
            payload: object = {"languages": self.languages}
        else:
            t0 = time.monotonic()
            try:
                code, payload = madlad.handle(self.translator, body, self.max_items)
            except Exception as e:                      # noqa: BLE001 — the card
                context.abort(self.status.INTERNAL, f"{type(e).__name__}: {e}")
            if code != 200:
                context.abort(getattr(self.status, REFUSALS.get(code, "INTERNAL")),
                              str(payload.get("error", payload)))
            items = body.get("items") or []
            print(f"translate {len(items)} items, "
                  f"{sum(len(it.get('text', '')) for it in items)} chars in "
                  f"{1000 * (time.monotonic() - t0):.0f} ms, GPU peak "
                  f"{payload.get('peak_mb')} MB", file=sys.stderr, flush=True)
        return self.pb.Reply(message=json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _settle(model_dir: str) -> None:
    madlad._release_host_memory()
    print(f"page cache dropped for {madlad._drop_file_cache(model_dir)} model files",
          file=sys.stderr, flush=True)


if __name__ == "__main__":
    lb.main(Servicer)
