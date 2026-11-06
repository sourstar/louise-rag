"""`hold embedder calibrate` — the tolerance a space can bear, measured
before the space exists. docs/07-commands.md §7.3; docs/03-checking.md
§3.3; docs/09-formats.md §9.3 (the engine), §9.12 (the set).

    louise hold embedder calibrate --encoder-weights D --engine FILE \\
        --against FILE … --ctx N [--set SHA256]

It embeds the platform's calibration set with the reference engine and
with every `--against` encoder, and reports the cosines between each pair
of rows and the tolerance tools/calibrate_space.py places from them: the
worst same-model cosine minus a three-sigma guard (`recommend_tau`). This
module WRAPS that harness rather than re-deriving it — the statistics,
the placement and the false-reject estimate are its, unchanged.

THE ENGINE FILE. 07 §7.3 fixes "a file of `weights` and `[engine]`, 09
§9.3's form", and the spec names no endpoint. A TOML file therefore
carries, at top level, the key a collection file's `[encoder]` block
uses for the same thing (07 §7.8): `url`, with `api` ("openai" unless
stated) and `model` beside it; `weights`; and the `[engine]` table in a
recipe's form (`variant` and `env` may be left out, app/hold/template.py).
`--engine FILE` may omit `weights` — `--encoder-weights` gives it — and a
file whose `weights` disagrees with the flag is refused.

WHY `--ctx`. The gate truncates every calibration text at the space's
`ctx` before it compares (09 §9.12, "truncated as a chunk is"), so a
tolerance measured over texts cut at another length measures a different
thing. There is no Codex yet to read `ctx` from, so it is given.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from . import calibration as cal
from .codex import CodexError, Engine, _validate_engine, engine_from_dict
from .template import _engine_from_recipe

#: The keys an engine file may carry at top level; anything else is a typo
#: that would otherwise be silently ignored.
FILE_KEYS = frozenset({"url", "api", "model", "weights", "engine"})
#: "vega": LocalAI's OpenAI-style endpoint with a `vega-` model, every
#: answer vouched for by VeGa's receipts (docs/notes/vega.md; VEGA_URL)
APIS = ("openai", "ollama", "vega")

#: The tolerance is written in the recipe as a decimal string (09 §9.3),
#: rounded DOWN so that it stays at or below what was measured (03 §3.3).
PLACES = Decimal("0.000001")


class CalibrateUnreachable(CodexError):
    """An endpoint could not be reached: nothing was measured (07 §7.7, 3)."""
    exit_code = 3


class CalibrateFailed(CodexError):
    """An endpoint answered, but not with vectors that can be compared."""
    exit_code = 1


@dataclass(frozen=True)
class Endpoint:
    """One encoder build: what it is (weights, engine) and where it answers."""
    file: str
    url: str
    api: str
    model: str
    weights: str
    engine: Engine

    def describe(self) -> dict[str, Any]:
        return {"file": self.file, "url": self.url, "weights": self.weights,
                "engine_id": self.engine.engine_id}


def read_endpoint(path: str | Path, *, weights: str | None = None) -> Endpoint:
    """An engine file, refused (exit 2) when it is not the form above."""
    p = Path(path)
    where = str(p)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CodexError(f"{where}: no such engine file") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise CodexError(f"{where}: not a TOML file: {e}") from None
    unknown = set(raw) - FILE_KEYS
    if unknown:
        raise CodexError(f"{where}: unknown key(s) {sorted(unknown)}; an engine file "
                         f"carries url, api, model, weights and [engine] (07 §7.3)")
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise CodexError(f"{where}: `url` names the endpoint that serves this engine, "
                         f"http(s)://…; the spec's form carries none, so the file "
                         f"states it (07 §7.3)")
    api = raw.get("api", "openai")
    if api not in APIS:
        raise CodexError(f"{where}: api is one of {APIS}, got {api!r}")
    model = raw.get("model", "")
    if not isinstance(model, str):
        raise CodexError(f"{where}: model is a string")
    stated = raw.get("weights")
    if weights is not None and stated is not None and stated != weights:
        raise CodexError(f"{where}: weights {stated} disagree with --encoder-weights "
                         f"{weights}")
    w = weights if weights is not None else stated
    if not isinstance(w, str) or not w.startswith("sha256:") or len(w) != 71:
        raise CodexError(f"{where}: weights is the sha256: digest of the weights "
                         f"(09 §9.3), got {w!r}")
    if "engine" not in raw:
        raise CodexError(f"{where}: no [engine] table (09 §9.3)")
    engine = engine_from_dict(_engine_from_recipe(raw["engine"], f"{where} [engine]"),
                              f"{where} [engine]")
    _validate_engine(engine, f"{where} [engine]")
    return Endpoint(where, url.rstrip("/"), api, model, w, engine)


def load_set(set_id: str | None) -> tuple[str, list[str]]:
    """The calibration set: the one the platform ships for text, or the one
    `--set` names, refused when this platform carries no set of that id."""
    shipped = sorted(cal.SETS_DIR.glob("*.jsonl"))
    for p in shipped:
        got, texts = cal.load(p.stem)
        if set_id is None and p.stem == "text":
            return got, texts
        if set_id is not None and got == set_id:
            return got, texts
    raise CodexError(
        f"this platform ships no calibration set {set_id or 'for text'} "
        f"({', '.join(p.name for p in shipped) or 'none'}); --set names one by "
        f"its sha256 (09 §9.12)")


def load_pairs(path: str | Path) -> tuple[str, list[tuple[str, str]]]:
    """A PAIR space's calibration set, from its file (09 §9.12, amended
    2026-10-08): the 512 title-and-abstract pairs
    tools/build_pair_calibration_set.py drew from the collection's records,
    checked; its id is `H` of the file. A pair space is calibrated in the form
    its pipeline writes — every lane embeds these as pairs."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise CodexError(f"{p}: no such pair set") from None
    return cal.set_id(data), cal.pairs_of(data)


Embed = Callable[[Endpoint, list], tuple[list[list[float]], set]]


def http_embed(ctx: int, *, document_instruction: str | None = None,
               set_id: str | None = None) -> Embed:
    """Embed through the existing client (invariant 7), the endpoint's own
    recipe, truncating at `ctx` as the gate does. Returns the vectors and
    the runs the answers stated (`Louise-Run`, 03 §3.3).

    `document_instruction`: the space's `space.document.instruction` (09
    §9.3, Codex version 11), prepended by the client as to every document,
    so the tolerance is measured on the inputs the gate will send.

    A CPU LANE IS THE NODE'S STORED CPU REFERENCE (operator, 2026-11-06: "I
    already asked to save the cpu reference values so that we don't have to
    repeat that time consuming part"). With `set_id` given, an endpoint
    whose run states the CPU is answered from `cpu_reference.obtain`: the
    store's vectors when kept, else computed one text a request, journalled
    and kept there. The collection's `hold embedder cpu-reference` then
    finds them instead of embedding the set again. `embed.kept` says, per
    file, which file answered and how."""
    import dataclasses

    import httpx

    from app import collections as collections_mod, config, embeddings, encoder_use, run_check
    from . import cpu_reference as cpu_ref
    from .embedder import GateDeferred
    kept: dict[str, dict[str, str]] = {}

    def stated_lane(ep: Endpoint, recipe, texts: list):
        """The run that answers the lane, held to the engine file BEFORE any
        text is embedded; None when the endpoint cannot say (not behind VeGa,
        not `/upstream/<model>`), and the lane is embedded as before.

        THE ENGINE FILE MUST BE THE RUN'S (2026-11-06): wikidata's chain kept
        engine files written on 2026-11-03 for the engine before (16acc89)
        while the live one was ea2417d91dcc. Calibrate measured for 1.5 h and
        wrote the files' engine; `collection create` refused it afterwards
        (G5, `check_reference_runs`). The same check, here, refuses in the
        first second."""
        from . import codex as cx
        from .runs import RunRefused
        probe = run_check.PAIR_PROBE if texts and isinstance(texts[0], tuple) else run_check.PROBE
        try:
            stated = run_check.stated_run(recipe, probe_item=probe)
        except RunRefused:
            return None
        bad = []
        got_engine = cx.engine_from_dict(stated.engine, "engine").engine_id
        if got_engine != ep.engine.engine_id:
            bad.append(f"engine {got_engine[:23]}… (the file's is {ep.engine.engine_id[:23]}…)")
        if ep.weights and stated.weights != ep.weights:
            bad.append(f"weights {stated.weights[:23]}… (the file's are {ep.weights[:23]}…)")
        if bad:
            raise CodexError(
                f"{ep.file}: the run {stated.run_id[:23]}… that answers at {ep.url} has "
                f"{' and '.join(bad)}. A tolerance is measured for the engine its file names: "
                f"write the file from `louise hold embedder run`. Nothing was measured")
        return stated

    def cpu_lane(ep: Endpoint, recipe, texts: list, stated) -> tuple[list[list[float]], set] | None:
        """The lane from the node's store when the endpoint answers on the
        CPU; None for a card, which is embedded as before."""
        if stated is None or stated.driver != "cpu":
            return None
        cpu = dataclasses.replace(recipe, batch=1)
        timeout = cpu_ref.request_timeout(ctx, config.EMBED_CONCURRENCY)
        store = cpu_ref.node_directory(cpu_ref.node_key(
            weights=stated.weights, set_id=set_id or "", ctx=ctx,
            instruction=document_instruction, role="document"))
        got = cpu_ref.obtain(store, "document", set_id=set_id or "", texts=texts,
                             run_id=stated.run_id,
                             run={"driver": stated.driver, "engine": stated.engine,
                                  "weights": stated.weights},
                             embed=run_check.answers(cpu, name="calibrate", timeout=timeout),
                             one=run_check.answers(cpu, how="one", name="calibrate",
                                                   timeout=timeout))
        kept[ep.file] = {"how": got.how, "path": str(got.path), "run": got.run_id}
        return got.vectors, {got.run_id}

    def embed(ep: Endpoint, texts: list) -> tuple[list[list[float]], set]:
        recipe = collections_mod.EmbedRecipe(
            url=ep.url, api=ep.api,
            model=ep.model or collections_mod.model_named_by(ep.url) or "",
            dtype="f32", ctx=ctx, document_instruction=document_instruction)
        runs: set = set()
        try:
            # the calibration set in the space's form: texts, or a pair
            # space's pairs, sent as pairs (09 §9.12, amended 2026-10-08).
            # Leased for the whole lane (encoder_use.held): unleased, the
            # worker's idle sweep stopped a Nemotron calibration's CPU twin
            # every minute, mid-request (2026-11-03), as it had cpu-reference's
            with encoder_use.held(recipe, holder=encoder_use.holder("hold embedder calibrate")):
                stated = stated_lane(ep, recipe, texts) if set_id is not None else None
                lane = cpu_lane(ep, recipe, texts, stated)
                if lane is not None:
                    return lane
                return embeddings.embed_calibration(recipe, texts, runs=runs), runs
        except (GateDeferred, cpu_ref.Deferred) as e:
            raise CalibrateUnreachable(f"{ep.file}: {e}; nothing was measured, retry when it "
                                       f"answers (the CPU lane's journal is kept)") from e
        except cpu_ref.NotReproducible as e:
            raise CalibrateFailed(f"{ep.file}: {e}") from e
        except httpx.TransportError as e:
            raise CalibrateUnreachable(
                f"{ep.file}: the encoder at {ep.url} is unreachable ({e}); "
                f"nothing was measured, retry when it answers") from e
        except embeddings.ReceiptDeferred as e:
            # api = "vega": no receipt to vouch for the answers yet — VeGa
            # away or VEGA_URL unset — so nothing measured (docs/notes/vega.md)
            raise CalibrateUnreachable(
                f"{ep.file}: {e}; nothing was measured, retry when it answers") from e
        except embeddings.EndpointBusy as e:
            raise CalibrateUnreachable(
                f"{ep.file}: the encoder at {ep.url} is not serving "
                f"({f'HTTP {e.status}' if e.status else e}); retry when it answers") from e
        except embeddings.EndpointMisconfigured as e:
            # the file names the wrong endpoint: the operator's to fix, and
            # waiting will not — a refusal of the file, never a deferral
            raise CalibrateFailed(f"{ep.file}: the encoder at {ep.url}: {e}") from e
        except RuntimeError as e:
            status = getattr(getattr(e.__cause__, "response", None), "status_code", 0)
            if status in (502, 503, 504):
                raise CalibrateUnreachable(
                    f"{ep.file}: the encoder at {ep.url} is not serving "
                    f"(HTTP {status}); retry when it answers") from e
            raise CalibrateFailed(f"{ep.file}: the encoder at {ep.url}: {e}") from e

    embed.kept = kept   # type: ignore[attr-defined]
    return embed


def tolerance_of(tau: float) -> str:
    """`space.tolerance` as the recipe writes it: a decimal string, rounded
    down so it never exceeds the measurement."""
    d = Decimal(repr(float(tau))).quantize(PLACES, rounding=ROUND_FLOOR)
    return format(d, "f")


def measure(reference: Endpoint, against: Sequence[Endpoint], *,
            set_id: str, texts: Sequence[cal.Item], embed: Embed,
            n_t3: int = 16) -> dict[str, Any]:
    """Embed the set with every endpoint, then hand the vectors to
    tools/calibrate_space.py's `calibrate` with the reference first — its
    live path, where every other lane is same-model, different-backend."""
    import numpy as np

    from tools import calibrate_space as cs

    if not against:
        raise CodexError("calibrate measures the reference engine against at least one "
                         "other: --against FILE (07 §7.3)")
    files = [reference.file, *(a.file for a in against)]
    if len(set(files)) != len(files):
        raise CodexError("an engine file is named twice; each lane is one file")

    # Lanes are keyed by position, never by path: the harness reads a key
    # starting `reference/` or `same/` as its synthetic naming convention,
    # and a relative path may well start so.
    lanes: dict[str, Any] = {}
    stated: dict[str, list] = {}
    key = {ep.file: f"#{i}" for i, ep in enumerate((reference, *against))}
    for ep in (reference, *against):
        vectors, runs = embed(ep, list(texts))
        v = np.asarray(vectors, dtype=np.float64)
        if v.ndim != 2 or v.shape[0] != len(texts) or v.shape[1] == 0:
            raise CalibrateFailed(f"{ep.file}: {ep.url} returned {v.shape} vectors for "
                                  f"{len(texts)} calibration texts")
        if not np.all(np.isfinite(v)) or np.any(np.linalg.norm(v, axis=1) == 0):
            raise CalibrateFailed(f"{ep.file}: {ep.url} returned a zero or non-finite vector")
        lanes[key[ep.file]] = v
        stated[ep.file] = sorted(r or "" for r in runs)
    width = lanes[key[reference.file]].shape[1]
    for a in against:
        if lanes[key[a.file]].shape[1] != width:
            raise CodexError(
                f"{a.file}: vectors of width {lanes[key[a.file]].shape[1]}, the reference's "
                f"are {width}: a different width is a different space, not a build "
                f"within this one's tolerance")

    result = cs.calibrate(lanes, {"synthetic": False, "set": set_id,
                                  "texts": len(texts)}, n_t3=n_t3)
    rec = result["recommendation"]
    pairs = {p["producer_class"]: p for p in result["calibration"]["pairs"]}
    by_file = {a.file: pairs[key[a.file]] for a in against}
    return {
        "set": set_id,
        "texts": len(texts),
        "dim": int(width),
        "reference": {**reference.describe(), "runs": stated[reference.file]},
        "against": [{**a.describe(), "runs": stated[a.file],
                     **({"cpu_reference": getattr(embed, "kept", {})[a.file]}
                        if a.file in getattr(embed, "kept", {}) else {}),
                     "n_pairs_measured": by_file[a.file]["n_pairs_measured"],
                     "cos": by_file[a.file]["cos"],
                     "p_miss_honest": by_file[a.file]["p_miss_honest"]}
                    for a in against],
        "recommendation": {k: rec[k] for k in ("tau", "guard", "p_miss_honest",
                                               "false_reject_at_n", "detect_5pct_at_n",
                                               "n_t3")},
        "adversary_epsilon_budget": result["epsilon"]["adversary_epsilon_budget"],
        "tolerance": tolerance_of(rec["tau"]),
    }
