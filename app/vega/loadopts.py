"""A LocalAI load call, as docs/09-formats.md §9.3's engine `options`.

LocalAI starts a backend with `--addr` alone and sends everything that shapes
its numbers — context, batch, GPU layers, threads, pooling — in the gRPC
`LoadModel` call, after completing the model's YAML with defaults of its own
(docs/notes/model-pool-over-localai.md §10). What the backend received is listed by
`GET /system` as `load_options` (deploy/localai/patches P1). This module makes
that record the engine's `options`, byte for byte as 09 §9.3 fixes them:

- the weights file (`ModelFile`) is written `${WEIGHTS}`, and the runner's
  models directory (`ModelPath`) `${MODELS}`;
- every number LocalAI wrote with a point or an exponent is its decimal
  string, without exponent — read with `parse_float=Decimal`, so the digits
  are LocalAI's and never a float's re-rendering;
- a SECOND file — a LoRA adapter, a projector, a draft model, each field
  of `SECOND_FILE_FIELDS` — is written `${WEIGHTS:<field>}` (`[i]` for a
  list) and hashed into the weights with the main file and the LoRA scales
  (`runs.load_weights_digest`; 09 §9.3 amended 2026-10-31, operator: a LoRA
  "decouples the embedder from a tuning"). It must reach the backend as an
  absolute path, so that the file hashed is the file opened: a relative
  name is resolved by each backend its own way and is refused here. Any
  other absolute path is still refused, by the Codex's own rule
  (`validate_options`).
- the model's `env:`, which LocalAI copies into the load call as `EnvVars`
  (`core/backend/options.go` L579), is the engine's `env` and is written
  there once, not again among the options (09 §9.3, operator 2026-10-03):
  the variables set for an engine are `env`'s, paths included, as
  `NEO_CACHE_DIR` is on every engine today.

NOTHING HERE DECIDES WHAT MATTERS. Every option LocalAI sent is kept: an
option believed inert and dropped would be a declaration where 02 §2.4 asks
for a measurement (model-pool-over-localai §10, point 3).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from ..hold.codex import CodexError, OPTION_PLACEHOLDERS, validate_options

WEIGHTS_PLACEHOLDER, MODELS_PLACEHOLDER = OPTION_PLACEHOLDERS

#: `backend.proto`'s ModelOptions fields that name a file beside the weights
#: (LocalAI 58830f7, `core/backend/options.go` L481–L574): MMProj is joined
#: to the models directory, the others are passed as written — a name the
#: path rule alone could not see.
SECOND_FILE_FIELDS = ("MMProj", "LoraAdapter", "LoraAdapters", "LoraBase", "DraftModel",
                      "CLIPModel", "ControlNet", "AudioPath", "Tokenizer")


#: the options that say how much of a second file the load applies: part
#: of the weights (`runs.load_weights_digest`), so of the space, not only
#: of the engine
SHAPING_FIELDS = ("LoraScale", "LoraScales")


class Unstatable(ValueError):
    """The load cannot be written as 09 §9.3's options, so no run is stated:
    search with it, write nothing through it (03 §3.3)."""


@dataclass(frozen=True)
class Canon:
    options: dict[str, Any]       # 09 §9.3's `options`, ready to hash
    weights_path: str             # the file the backend loaded, to hash (09 §9.12)
    env: dict[str, str]           # the model's `env:` (`EnvVars`): the engine's `env`, not an option
    #: every SECOND file the load names (a LoRA, a projector, a draft model),
    #: by the placeholder that stands for it in `options` — `${WEIGHTS:LoraAdapters[0]}`
    #: — to its path: all hashed into the run's weights (09 §9.3, amended
    #: 2026-10-31, operator: "Extend for future use in any case … a lora is a
    #: smart way to decouple the embedder from a tuning")
    files: dict[str, str] = field(default_factory=dict)
    #: the options that say how much of a second file is applied (the LoRA
    #: scales), as `options` writes them: in the weights digest beside the
    #: files, since they change what the weights compute
    shaping: dict[str, Any] = field(default_factory=dict)


def second_file_placeholder(name: str, index: int | None = None) -> str:
    """`${WEIGHTS:LoraAdapter}`, `${WEIGHTS:LoraAdapters[0]}`: the option's
    value in 09 §9.3's `options`, and its key in the weights digest's map."""
    return "${WEIGHTS:" + name + ("" if index is None else f"[{index}]") + "}"


def _second_file(name: str, value: Any, index: int | None) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise Unstatable(f"its {name}{'' if index is None else f'[{index}]'} is {value!r}: a second "
                         f"file is named by its absolute path in the model's YAML, so that the "
                         f"file hashed is the file the backend opens (09 §9.3)")
    return value


def parse(text: str | bytes) -> Any:
    """LocalAI's JSON with every non-integer number as a Decimal of the
    digits it wrote."""
    return json.loads(text, parse_float=Decimal)


def _decimal(d: Decimal) -> str:
    """`1e-05` → "0.00001", `10000.50` → "10000.5", `1E+6` → "1000000"."""
    text = format(d.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _canon(value: Any, path: str) -> Any:
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, float):
        raise TypeError(f"{path}: read LocalAI's JSON with loadopts.parse, so that its "
                        f"digits are kept; a float here is already a re-rendering")
    if isinstance(value, dict):
        return {k: _canon(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_canon(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def canonical(raw: Any) -> Canon:
    if not isinstance(raw, dict) or not raw:
        raise Unstatable("LocalAI lists no load options for it; VeGa needs them to state a "
                         "run (deploy/localai/patches P1 applied to the LocalAI it runs?)")
    weights = raw.get("ModelFile")
    if not isinstance(weights, str) or not weights.startswith("/"):
        raise Unstatable(f"its load options name no weights file (ModelFile={weights!r}), "
                         f"so there is nothing to hash")
    # A SECOND FILE IS PART OF THE WEIGHTS (09 §9.3, amended 2026-10-31): its
    # path becomes a placeholder in the options and the file goes into the
    # weights digest under that placeholder — it was a refusal, "cannot be
    # stated yet", because no digest covered it
    files: dict[str, str] = {}
    replaced: dict[str, Any] = {}
    for name in SECOND_FILE_FIELDS:
        value = raw.get(name)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            out = []
            for i, v in enumerate(value):
                files[second_file_placeholder(name, i)] = _second_file(name, v, i)
                out.append(second_file_placeholder(name, i))
            replaced[name] = out
        else:
            files[second_file_placeholder(name)] = _second_file(name, value, None)
            replaced[name] = second_file_placeholder(name)
    env = raw.get("EnvVars") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in env.items()):
        raise Unstatable(f"its EnvVars are not a map of names to strings: {env!r}")
    # 09 §9.3: the variables set for the engine are its `env`, stated once
    options = _canon({k: v for k, v in raw.items() if k != "EnvVars"}, "load_options")
    options.update(replaced)
    options["ModelFile"] = WEIGHTS_PLACEHOLDER
    if isinstance(raw.get("ModelPath"), str) and raw["ModelPath"]:
        options["ModelPath"] = MODELS_PLACEHOLDER
    try:
        validate_options(options, "load_options")
    except CodexError as e:
        raise Unstatable(str(e)) from None
    shaping = {k: options[k] for k in SHAPING_FIELDS if files and k in options}
    return Canon(options=options, weights_path=weights, env=dict(env), files=files,
                 shaping=shaping)


def changed(old: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> list[str]:
    """What differs between two runs' options, one line per option, for the
    event a new run records (model-pool-over-localai §10: "bge-m3: new run —
    NBatch 512 → 2048")."""
    old, new = dict(old or {}), dict(new or {})
    out = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            out.append(f"{key} {json.dumps(new[key], sort_keys=True)} (added)")
        elif key not in new:
            out.append(f"{key} {json.dumps(old[key], sort_keys=True)} (gone)")
        elif old[key] != new[key]:
            out.append(f"{key} {json.dumps(old[key], sort_keys=True)} → "
                       f"{json.dumps(new[key], sort_keys=True)}")
    return out
