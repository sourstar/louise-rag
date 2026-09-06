"""Chat-model access via Ollama (Oreste does the talking)."""
import json
import re
import httpx
from . import config

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)

def strip_think(text: str) -> str:
    return _THINK.sub("", text).strip()

def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1500,
         num_ctx: int = 16384) -> str:
    with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        r = client.post(f"{config.OLLAMA_URL}/api/chat", json={
            "model": config.CHAT_MODEL, "messages": messages, "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens,
                        "num_ctx": num_ctx},
        })
        r.raise_for_status()
        return strip_think(r.json()["message"]["content"])

def chat_stream(messages: list[dict], temperature: float = 0.3):
    """Yield content deltas."""
    with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        with client.stream("POST", f"{config.OLLAMA_URL}/api/chat", json={
            "model": config.CHAT_MODEL, "messages": messages, "stream": True,
            "think": False,
            "options": {"temperature": temperature, "num_ctx": 16384},
        }) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                delta = obj.get("message", {}).get("content", "")
                if delta:
                    yield delta
                if obj.get("done"):
                    return

def translate_query(query: str, target_langs: list[str]) -> dict[str, str]:
    """One prompt, N translations, JSON out. Failure degrades to no fan-out."""
    targets = [l for l in target_langs if l][:12]
    if not targets:
        return {}
    prompt = (
        "Translate the following search query into each of these ISO language "
        f"codes: {', '.join(targets)}.\n"
        f"Query: {query}\n"
        'Reply with ONLY a JSON object mapping language code to translation, '
        'e.g. {"fr": "...", "de": "..."}.'
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=600)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{"):]
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        return {k: str(v) for k, v in obj.items() if isinstance(v, str)}
    except Exception:
        return {}


def translate_texts(texts: list[str], lang: str) -> list[str]:
    """Translate several passages into ISO language `lang` in ONE prompt
    (STATUS 5j: the Ask page's "show results in"). Returns one string per
    input, or [] when the model failed — the caller then shows the
    originals. Numbered JSON in and out keeps passages from bleeding into
    each other; a passage the model dropped comes back as its original."""
    items = [t for t in texts]
    if not items or not lang:
        return []
    numbered = "\n\n".join(f"[{i + 1}] {t}" for i, t in enumerate(items))
    prompt = (
        f"Translate each numbered passage below into the language with ISO code "
        f"'{lang}'. Keep numbers, names, citations like [3] and formatting as they "
        f"are; do not summarise, do not add commentary.\n\n{numbered}\n\n"
        'Reply with ONLY a JSON object mapping each number to its translation, '
        'e.g. {"1": "...", "2": "..."}.'
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], temperature=0.0,
                   max_tokens=max(600, 2 * sum(len(t) for t in items) // 3 + 200),
                   num_ctx=16384)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{"):]
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        return []
    out = []
    for i, original in enumerate(items):
        value = obj.get(str(i + 1))
        out.append(value if isinstance(value, str) and value.strip() else original)
    return out
