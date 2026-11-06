"""Does LocalAI apply the sampling the chat benchmark sends? (operator,
2026-11-06: "Be absolutely sure that localai is applying your settings
otherwise the test is void.")

    python -m tools.measure.chat_eval probe --out DIR --model louise-chat-qwen3-8b-q8 \\
        [--installed /models/louise-chat-qwen3-8b-q8.yaml]

WHY A PROBE AND NOT THE FILE. What the engine runs is not what the model
file says (LocalAI's code, read 2026-11-06; docs/notes/nemotron-embed.md
"Sampling, Q8 first"):
- a family's defaults fill what LocalAI takes for unset, and a penalty of 0
  IS unset to it: a file's presence_penalty 0 on a model named "qwen3.5"
  becomes the family's 1.5 (core/config/inference_defaults.go);
- a gallery install writes the family's defaults into the installed file
  (LocalAI's "persist inference defaults where the loader reads them");
- a request's penalty of 0 is ignored (request.go merges on != 0), and a
  `repeat_last_n` of 0 disables every llama.cpp penalty.
So every value is checked by its EFFECT, on the node, through the body Ask
sends (`llm._request`, Ask's tools declared), never by reading a config.

THE CHECKS, each an answer compared with another (llama.cpp's sampler
chain: top_k, top_p and min_p cut the candidates before the temperature
draws, so a cut to one candidate IS greedy decoding):
- greedy repeats itself (temperature 0, twice): else nothing below decides;
- temperature: hot (1.5, seed 11) differs from greedy;
- seed: hot with seed 11 twice is the same, with seed 12 it differs;
- top_k 1, top_p 1e-6, min_p 0.999, each hot: the greedy answer;
- the file's penalties neutral: greedy, on free text and on a repetitive
  prompt, equals the same with presence 1e-6 (a request value LocalAI
  does apply, too small to move a float32 logit) — and if it equals
  presence 1.5 instead, that is said: the family's default is filling the
  file's 0;
- a presence penalty applied: presence 2.0 differs from presence 1e-6 on
  free text or on the repetitive prompt. Free text is the sensitive one
  (2026-11-06, München's first probes): told to repeat a word, Qwen3-8B is
  sure enough of every token that 2.0 moves none, while free text is full
  of near-ties; the live file, which states no `repeat_last_n`, is the
  negative control — it must fail here;
- max_tokens: 7 asked, 7 generated, finish "length";
- thinking off: no <think> and no reasoning in the answer;
- the context: a request past it is refused naming 16384 tokens.
A whole run of the benchmark refuses a model without a passing probe.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SEA = "Write a short paragraph about the sea."
REPEAT = "Write the word Louise 30 times, separated by commas, and nothing else."
HOT, SEED, TINY = 1.5, 11, 1e-6
CONTEXT = 16384
_CONTEXT_SAID = re.compile(r"context size \((\d+) tokens\)")

Post = Callable[[str, dict[str, Any]], tuple[int, Any]]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class ProbeError(RuntimeError):
    """An answer the probe could not get: the probe fails, it does not guess."""


def http_post(url: str, body: dict[str, Any]) -> tuple[int, Any]:
    """POST as llm.chat_tools does (the model pool's dispatch, its level
    headers), returning the status and the JSON, or the text."""
    import httpx

    from app import config
    from app.rage import dispatch
    from app.rage.dispatch import Level
    with dispatch.call(config.CHAT_URL, Level.CHAT, timeout=config.LLM_TIMEOUT), \
            httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        r = client.post(url, json=body, headers=dispatch.level_headers())
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def localai_version() -> str | None:
    import httpx

    from app import config
    try:
        r = httpx.get(f"{config.CHAT_URL}/version", timeout=10)
        return r.json().get("version") if r.status_code == 200 else None
    except Exception:   # noqa: BLE001 — the version is evidence, not a condition
        return None


def _body(prompt: str, sampling: dict[str, Any], max_tokens: int) -> tuple[str, dict[str, Any]]:
    """The body Ask sends: llm._request, Ask's tools declared."""
    from app import llm
    from app.ask import chat
    url, body = llm._request([{"role": "user", "content": prompt}], stream=False, temperature=None,
                             max_tokens=max_tokens, num_ctx=CONTEXT, sampling=sampling)
    body["tools"] = chat.tool_specs()
    return url, body


def _message(got: Any) -> dict[str, Any]:
    try:
        return got["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ProbeError(f"not a chat answer: {str(got)[:200]}") from None


def _said(got: Any) -> str:
    """What an answer said, to compare: its text and its tool calls."""
    msg = _message(got)
    return json.dumps({"content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []},
                      ensure_ascii=False, sort_keys=True)


def probe(model: str, post: Post = http_post) -> dict[str, Any]:
    """Every check, in order; a failed request fails the probe with its
    reason. `post(url, body) -> (status, json or text)`."""
    from app import config
    if config.CHAT_API != "openai":
        raise SystemExit("the probe checks LocalAI (CHAT_API openai); this node speaks "
                         f"{config.CHAT_API}")
    config.CHAT_MODEL = model
    checks: list[Check] = []

    def ask(prompt: str, sampling: dict[str, Any], max_tokens: int = 48) -> Any:
        status, got = post(*_body(prompt, sampling, max_tokens))
        if status != 200:
            raise ProbeError(f"{status}: {str(got)[:300]}")
        return got

    def short(said: str) -> str:
        return json.loads(said)["content"][:80].replace("\n", " ")

    try:
        greedy_raw = ask(SEA, {"temperature": 0})
        greedy = _said(greedy_raw)
        checks.append(Check("greedy repeats itself", _said(ask(SEA, {"temperature": 0})) == greedy,
                            short(greedy)))
        if not checks[-1].ok:
            return _result(model, checks)
        hot = {"temperature": HOT, "seed": SEED}
        h1, h2 = _said(ask(SEA, hot)), _said(ask(SEA, hot))
        h3 = _said(ask(SEA, {**hot, "seed": SEED + 1}))
        checks.append(Check("temperature reaches the engine", h1 != greedy, short(h1)))
        checks.append(Check("seed reaches the engine", h1 == h2 and h1 != h3,
                            "same seed same answer, another seed another" if h1 == h2 and h1 != h3
                            else f"seed {SEED} twice {'same' if h1 == h2 else 'DIFFERENT'}, "
                                 f"seed {SEED + 1} {'DIFFERENT' if h1 != h3 else 'the SAME'}"))
        hot_differs = checks[1].ok
        for name, cut in (("top_k", {"top_k": 1}), ("top_p", {"top_p": TINY}), ("min_p", {"min_p": 0.999})):
            got = _said(ask(SEA, {**hot, **cut}))
            checks.append(Check(f"{name} reaches the engine", hot_differs and got == greedy,
                                "the greedy answer" if got == greedy else
                                f"not the greedy answer: {short(got)}" if hot_differs else
                                "undecidable: the temperature did not reach the engine"))
        seen: dict[str, dict[str, str]] = {}
        for label, prompt, n in (("free text", SEA, 48), ("repetition", REPEAT, 120)):
            seen[label] = {"plain": greedy if prompt == SEA else _said(ask(prompt, {"temperature": 0}, n)),
                           "tiny": _said(ask(prompt, {"temperature": 0, "presence_penalty": TINY}, n)),
                           "strong": _said(ask(prompt, {"temperature": 0, "presence_penalty": 2.0}, n)),
                           "family": _said(ask(prompt, {"temperature": 0, "presence_penalty": 1.5}, n))}
        filled = [k for k, v in seen.items() if v["plain"] != v["tiny"]]
        checks.append(Check("the file's penalties are neutral", not filled,
                            "no penalty unless sent (free text and repetition)" if not filled else
                            f"penalised on {', '.join(filled)} as by presence 1.5: a family default fills "
                            f"the file's 0" if all(seen[k]["plain"] == seen[k]["family"] for k in filled)
                            else f"penalised on {', '.join(filled)} by an unknown value"))
        moved = [k for k, v in seen.items() if v["strong"] != v["tiny"]]
        checks.append(Check("a presence penalty reaches the engine", bool(moved),
                            f"2.0 changed the answer on {', '.join(moved)}" if moved else
                            "2.0 changed nothing on free text or repetition: penalties disabled "
                            "(repeat_last_n 0?) or dropped"))
        capped = ask(SEA, {"temperature": 0}, 7)
        used = (capped.get("usage") or {}).get("completion_tokens")
        finish = capped["choices"][0].get("finish_reason")
        checks.append(Check("max_tokens reaches the engine", used == 7 and finish == "length",
                            f"{used} tokens, finish {finish}"))
        msg = _message(greedy_raw)
        thought = msg.get("reasoning_content") or msg.get("reasoning") or ""
        checks.append(Check("thinking is off", "<think>" not in (msg.get("content") or "") and not thought,
                            "no reasoning" if not thought else f"reasoning: {thought[:80]}"))
        status, got = post(*_body("Louise " * 20000, {"temperature": 0}, 8))
        m = _CONTEXT_SAID.search(str(got))
        said = int(m.group(1)) if m else None
        checks.append(Check(f"the context is {CONTEXT} tokens", status == 400 and said == CONTEXT,
                            f"{status}: {m.group(0) if m else str(got)[:160]}"))
    except ProbeError as e:
        checks.append(Check("every probe request answered", False, str(e)))
    return _result(model, checks)


def _result(model: str, checks: list[Check]) -> dict[str, Any]:
    return {"model": model, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "passed": bool(checks) and all(c.ok for c in checks),
            "checks": [asdict(c) for c in checks]}


def path(out: Path, model: str) -> Path:
    return out / "probes" / f"{model}.json"


def write(out: Path, result: dict[str, Any], installed: Path | None = None,
          version: str | None = None) -> Path:
    """The probe's result beside the runs, with the LocalAI version and the
    installed model file as the evidence of what was probed."""
    result = {**result, "localai_version": version}
    if installed is not None:
        result["installed_file"] = {"path": str(installed), "text": installed.read_text(encoding="utf-8")}
    p = path(out, result["model"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def require(out: Path, model: str) -> None:
    """A whole run, or a pilot, on a model whose probe has not passed is
    void (operator, 2026-11-06): refused, naming the failed checks."""
    p = path(out, model)
    if not p.exists():
        raise SystemExit(f"{model}: no sampling probe (`chat_eval probe --model {model}`): "
                         f"whether LocalAI applies the settings is unknown, and the test would be void")
    got = json.loads(p.read_text(encoding="utf-8"))
    if not got.get("passed"):
        failed = "; ".join(f"{c['name']} ({c['detail']})" for c in got.get("checks", []) if not c["ok"])
        raise SystemExit(f"{model}: the sampling probe failed — {failed}. The test would be void")


def table(result: dict[str, Any]) -> str:
    lines = [f"{result['model']}: {'PASSED' if result['passed'] else 'FAILED'}", "",
             "| check | ok | what was seen |", "|---|---|---|"]
    lines += [f"| {c['name']} | {'yes' if c['ok'] else '**NO**'} | {c['detail']} |" for c in result["checks"]]
    return "\n".join(lines)
