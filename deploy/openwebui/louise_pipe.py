"""
title: Louise
description: Louise's search and answer in Open WebUI — choose the collections and annotations per chat; the sources link to Louise's reader.
version: 1
required_open_webui_version: 0.6.0
license: same as Louise
"""
# An Open WebUI "pipe" function (operator, 2026-10-22: "an OpenWebUI equivalent
# of our ask page … selecting knowledge bases equivalent to collections and
# annotations"). Imported once by an Open WebUI admin: Admin Panel → Functions
# → + → paste this file (or Import), then set the Valves. It shows one model
# per Louise collection plus "all collections"; each person can choose several
# collections, the annotations, the answer's language and how many results in
# the chat's Controls → Valves. One search per question: Louise returns the
# hits with the answer (`sources: true`, 07 §7.4), and they become Open WebUI's
# sources, numbered as the answer's [n].
#
# Self-contained: it imports nothing of Louise and runs inside Open WebUI,
# which ships httpx and pydantic. Tested by tests/test_openwebui_pipe.py.
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from pydantic import BaseModel, Field

ALL = "all"


class Pipe:
    class Valves(BaseModel):
        LOUISE_URL: str = Field("http://louise:30042",
                                description="Where Open WebUI reaches Louise (its own network)")
        PUBLIC_URL: str = Field("", description="Where a browser reaches Louise, for the source links; "
                                                "empty = LOUISE_URL")
        LOUISE_TOKEN: str = Field("", description="A Louise key for this Open WebUI: `louise user add openwebui`")
        TIMEOUT_S: int = Field(300, description="How long one answer may take")

    class UserValves(BaseModel):
        COLLECTIONS: str = Field("", description="Collections to search, comma-separated; "
                                                 "empty = the model's (all, or the one it names)")
        ANNOTATIONS: str = Field("all", description="all, none, or annotations collections, comma-separated")
        ANSWER_IN: str = Field("", description="Answer language (en, it, …); empty = the question's")
        RESULTS: int = Field(10, ge=1, le=100, description="Sources per answer")
        ALL_LANGUAGES: bool = Field(False, description="Do not prefer sources in the question's language")
        TOKEN: str = Field("", description="Your own Louise key, if you have one; else the admin's")

    def __init__(self) -> None:
        self.type = "manifold"
        self.name = "Louise: "
        self.valves = self.Valves()

    # ---- the HTTP client (replaced in tests) ------------------------------------

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout)

    def _sync_client(self) -> httpx.Client:
        return httpx.Client(timeout=15)

    def _headers(self, token: str = "") -> dict[str, str]:
        key = token or self.valves.LOUISE_TOKEN
        return {"Authorization": f"Bearer {key}"} if key else {}

    # ---- the models -----------------------------------------------------------

    def pipes(self) -> list[dict[str, str]]:
        """"all collections" and one per collection, from Louise's own list —
        nothing to type (RULE 2)."""
        out = [{"id": ALL, "name": "all collections"}]
        try:
            with self._sync_client() as c:
                r = c.get(f"{self.valves.LOUISE_URL.rstrip('/')}/v1/models", headers=self._headers())
                r.raise_for_status()
                ids = [m["id"] for m in r.json().get("data") or []]
        except Exception as e:   # noqa: BLE001 — a model that says why, instead of none
            return out + [{"id": "unreachable", "name": f"(Louise not reachable: {type(e).__name__})"}]
        out += [{"id": i.split("/", 1)[1], "name": i.split("/", 1)[1]} for i in ids if i.startswith("louise/")]
        return out

    # ---- one question --------------------------------------------------------

    def request_body(self, body: dict, uv: "Pipe.UserValves") -> dict[str, Any]:
        """What Louise is asked: the chat's turns, and the collections and
        annotations the person chose (or the model names)."""
        sub = str(body.get("model") or "").split(".", 1)[-1]
        chosen = [c.strip() for c in uv.COLLECTIONS.split(",") if c.strip()]
        collections = chosen or (None if sub in (ALL, "", "unreachable") else [sub])
        ann = uv.ANNOTATIONS.strip().lower()
        annotations = None if ann in ("", ALL) else ([] if ann == "none" else
                                                     [a.strip() for a in uv.ANNOTATIONS.split(",") if a.strip()])
        out: dict[str, Any] = {
            "model": "louise", "stream": True, "sources": True, "limit": uv.RESULTS,
            "all_languages": uv.ALL_LANGUAGES,
            "messages": [m for m in body.get("messages") or [] if m.get("role") in ("user", "assistant")],
        }
        if collections is not None:
            out["collections"] = collections
        if annotations is not None:
            out["annotations"] = annotations
        if uv.ANSWER_IN.strip():
            out["answer_lang"] = uv.ANSWER_IN.strip().lower()
        return out

    def citation(self, s: dict) -> dict:
        """One Louise source as an Open WebUI source: the passage, the work's
        other editions and the claims beside it, linked to Louise's reader
        (or to the origin when this node cannot open it)."""
        base = (self.valves.PUBLIC_URL or self.valves.LOUISE_URL).rstrip("/")
        url = f"{base}{s['reader']}" if s.get("reader") else (s.get("origin_url") or "")
        label = f"[{s['n']}] {s.get('title') or s.get('uid') or '?'} — {s.get('collection')} ({s.get('lang') or '?'})"
        text = str(s.get("text") or "")
        if s.get("editions"):
            text += "\n\nAlso in: " + " · ".join(f"{e.get('title') or e.get('uid')} ({e.get('lang') or '?'})"
                                               for e in s["editions"])
        if s.get("annotations"):
            text += "\n\nAlso: " + " · ".join(f"{a.get('type')} — {a.get('title') or a.get('uid')} "
                                             f"({a.get('lang') or '?'})" for a in s["annotations"])
        return {"source": {"id": url or label, "name": label, **({"url": url} if url else {})},
                "document": [text],
                "metadata": [{"source": url or label, "name": label, **({"url": url} if url else {})}]}

    async def pipe(self, body: dict, __user__: dict | None = None,
                   __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
                   __task__: str | None = None) -> AsyncIterator[str] | str:
        if __task__:
            # Open WebUI's titles, tags and follow-ups: never a Louise search
            # (a search is seconds of a reranker); set a Task Model to get them
            return _task_answer(__task__, body)
        uv = (__user__ or {}).get("valves") or self.UserValves()
        payload = self.request_body(body, uv)
        return self._stream(payload, uv.TOKEN, __event_emitter__)

    async def _stream(self, payload: dict, token: str,
                      emit: Callable[[dict], Awaitable[None]] | None) -> AsyncIterator[str]:
        async def say(event: dict) -> None:
            if emit is not None:
                await emit(event)

        await say({"type": "status", "data": {"description": "Searching Louise…", "done": False}})
        url = f"{self.valves.LOUISE_URL.rstrip('/')}/v1/chat/completions"
        try:
            async with self._client(self.valves.TIMEOUT_S) as c:
                async with c.stream("POST", url, json=payload, headers=self._headers(token)) as r:
                    if r.status_code == 401:
                        await say({"type": "status", "data": {"description": "Louise refused the key", "done": True}})
                        yield ("Louise refused the key. The Open WebUI admin sets LOUISE_TOKEN in this "
                               "function's Valves (a key from `louise user add openwebui`), or you set "
                               "your own in Controls → Valves → TOKEN.")
                        return
                    if r.status_code >= 400:
                        detail = (await r.aread()).decode("utf-8", "replace")[:500]
                        await say({"type": "status", "data": {"description": f"Louise: {r.status_code}", "done": True}})
                        yield f"Louise answered {r.status_code}: {detail}"
                        return
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        sources = chunk.get("louise_sources")
                        if sources is not None:
                            for s in sources:
                                await say({"type": "citation", "data": self.citation(s)})
                            await say({"type": "status", "data": {
                                "description": f"{len(sources)} sources", "done": True}})
                        for choice in chunk.get("choices") or []:
                            text = (choice.get("delta") or {}).get("content")
                            if text:
                                yield text
        except httpx.HTTPError as e:
            await say({"type": "status", "data": {"description": "Louise not reachable", "done": True}})
            yield f"Louise is not reachable at {self.valves.LOUISE_URL} ({type(e).__name__})."


def _task_answer(task: str, body: dict) -> str:
    first = next((str(m.get("content") or "") for m in body.get("messages") or [] if m.get("role") == "user"), "")
    if "title" in task:
        return json.dumps({"title": first[:60] or "Louise"})
    if "tags" in task:
        return json.dumps({"tags": []})
    if "follow" in task:
        return json.dumps({"follow_ups": []})
    return ""
