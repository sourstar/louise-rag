"""An origin's ASYNCHRONOUS EXPORT as an acquisition file (09 §9.17, amended
2026-10-13; docs/notes/normattiva-full-corpus.md §2–4).

A `RemoteFile` whose `remote` is `export:<json>` is not served at a URL: the
origin BUILDS it on request. Normattiva's open-data API (manual rev.
09/01/2025 §1.3.5–1.3.7), measured on the node 2026-10-13:

    POST ricerca-asincrona/nuova-ricerca   {formato, tipoRicerca, parametriRicerca…}
         → 202, the job's token (a UUID, as text or as {"token": …})
    PUT  ricerca-asincrona/conferma-ricerca {"token": …}      → 200
    GET  ricerca-asincrona/check-status/<token>               → 200 while it runs,
         303 with `x-ipzs-location` = the zip's URL when done (about 50 s
         for one act); any other status is the origin's refusal
    GET  <x-ipzs-location>                                     → the zip, whose
         members are named exactly as the pre-packaged collections' are

The json names the PARTITION the job exports — a year of acts, in one of
two forms: `O`, the originals (`ORIGINALE_V0` members), or `V`, the
versions in force on the day of the pass (`dataVigenza`). One version per
job is all the origin offers outside its pre-packaged collections; the
collection declares what that leaves out (the note's §3, design A).

WHAT MAKES A PASS CHEAP (invariant: a delta is honest or it is a re-fetch):
`ricerca/aggiornati` lists the acts the origin updated between two dates.
An export file already on disk is ADOPTED when the feed, read since the
file's own mtime, names no act of its partition; it is re-exported when
one is named, and left standing (deferred) when the feed cannot be read.

Every wait here is bounded: a job that is not done in JOB_BOUND_S is this
pass's failure of the file, worded as a timeout so the engine's `transient`
test retries it next pass; the bound stop event (SIGTERM, `node pause`) is
honoured between polls."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx

from ..transfer.manager import current_handle
from . import budget
from .spec import RemoteFile, TransferAborted, TransportError

PREFIX = "export:"
#: the AKN export; the origin's other formats (XML, JSON, URI, HTML) are not
#: the corpus form the plugin reads
FORMAT = "AKN"
#: the poll interval: the origin took about 50 s for one act (2026-10-13)
POLL_S = 10.0
#: one job's bound, from its confirmation to its 303
JOB_BOUND_S = 3 * 3600.0
#: one HTTP exchange's bound; the download streams past it chunk by chunk
HTTP_TIMEOUT_S = 120.0
#: the update feed is read once per hour of file age per pass, not per file
_FEED_GRAIN_S = 3600
_TOKEN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def encode(partition: dict) -> str:
    """The `remote` of an export file: `export:` and the partition, as canonical
    json so two specs naming one partition name one remote."""
    return PREFIX + json.dumps(partition, sort_keys=True, separators=(",", ":"))


def is_export(f: RemoteFile) -> bool:
    return bool(f.remote) and str(f.remote).startswith(PREFIX)


def partition_of(f: RemoteFile) -> dict:
    return json.loads(str(f.remote)[len(PREFIX):])


def api_root(endpoint_url: str) -> str:
    """The API's root from the endpoint's URL: Normattiva's spec names the
    download endpoint (`…/v1/api/v1/collections/download`); the job routes
    are its siblings under `…/v1/api/v1`."""
    url = endpoint_url.rstrip("/")
    marker = "/collections/download"
    return url[: -len(marker)] if url.endswith(marker) else url


def body_for(partition: dict, *, vigenza_day: str | None) -> dict:
    """The job's request for a partition `{"year": 1990, "form": "O"|"V"[,
    "type": "LEGGE"]}` — the advanced search's own parameters (manual
    §1.3.2) under `parametriRicerca`, as `nuova-ricerca` takes them."""
    year = int(partition["year"])
    params: dict = {"dataInizioEmanazione": f"{year:04d}-01-01",
                    "dataFineEmanazione": f"{year:04d}-12-31",
                    "titoloContainsType": "ALL_WORDS", "testoContainsType": "ALL_WORDS",
                    "limitaAnniVigenza": False}
    if partition.get("type"):
        params["denominazioneAtto"] = str(partition["type"])
    body: dict = {"formato": FORMAT, "tipoRicerca": "A", "modalita": "C",
                  "parametriRicerca": params}
    if partition.get("form") == "V":
        if not vigenza_day:
            raise ValueError("a VIGENTE export needs the day it is in force on")
        body["dataVigenza"] = vigenza_day
    return body


def _stop_check() -> None:
    handle = current_handle()
    stop = getattr(handle, "stop", None) if handle is not None else None
    if stop is not None and stop.is_set():
        raise TransferAborted("export: stopped while the origin built the job")


def _token_of(r: httpx.Response) -> str:
    text = r.text.strip()
    try:
        obj = r.json()
        if isinstance(obj, dict) and obj.get("token"):
            text = str(obj["token"])
    except ValueError:
        pass
    m = _TOKEN.search(text)
    if m is None:
        raise TransportError(f"export: nuova-ricerca answered no token: {text[:200]}")
    return m.group(0)


def run(api: str, body: dict, dest: Path, *, headers: dict | None = None,
        poll_s: float | None = None, bound_s: float | None = None,
        charge=budget.acquire_bytes, sleep=time.sleep) -> int:
    """One export job, start to zip: returns the bytes written to `dest`.
    `poll_s` and `bound_s` default to the module's values AT CALL TIME, so
    a test (or an operator's tuning) that sets them is honoured."""
    poll_s = POLL_S if poll_s is None else poll_s
    bound_s = JOB_BOUND_S if bound_s is None else bound_s
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json, text/plain, */*")
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, headers=headers) as client:
            r = client.post(f"{api}/ricerca-asincrona/nuova-ricerca", json=body)
            if r.status_code not in (200, 202):
                raise TransportError(f"export: nuova-ricerca {r.status_code}: {r.text[:200]}")
            token = _token_of(r)
            r = client.put(f"{api}/ricerca-asincrona/conferma-ricerca", json={"token": token})
            if r.status_code != 200:
                raise TransportError(f"export: conferma-ricerca {r.status_code}: {r.text[:200]}")
            started = time.monotonic()
            while True:
                r = client.get(f"{api}/ricerca-asincrona/check-status/{token}")
                location = r.headers.get("x-ipzs-location")
                if r.status_code == 303 or location:
                    if not location:
                        raise TransportError(f"export: check-status 303 named no location for {token}")
                    break
                if r.status_code != 200:
                    raise TransportError(f"export: check-status {r.status_code}: {r.text[:200]}")
                if time.monotonic() - started > bound_s:
                    # worded for engine.transient: the next pass asks again
                    raise TransportError(f"export: job {token} not done in {bound_s:.0f} s "
                                         f"(connection timed out)")
                _stop_check()
                sleep(poll_s)
            with client.stream("GET", location, follow_redirects=True) as r:
                if r.status_code != 200:
                    raise TransportError(f"export: download {r.status_code} from {location}")
                with open(part, "wb") as out:
                    for chunk in r.iter_bytes(1 << 16):
                        out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                        charge(len(chunk))
    except httpx.HTTPError as e:
        raise TransportError(f"export: {type(e).__name__}: {e}") from e
    size = part.stat().st_size
    os.replace(part, dest)
    return size


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


_feed_cache: dict[tuple[str, int], set[tuple[str, int]]] = {}


def updated_since(api: str, since: float, *, headers: dict | None = None,
                  now: float | None = None) -> set[tuple[str, int]]:
    """`(denominazioneAtto, annoProvvedimento)` of every act the origin
    updated since `since` (epoch seconds), by `ricerca/aggiornati` (manual
    §1.3.10). Read once per hour of `since` per process."""
    key = (api, int(since // _FEED_GRAIN_S))
    hit = _feed_cache.get(key)
    if hit is not None:
        return hit
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json, text/plain, */*")
    body = {"dataInizioAggiornamento": _iso(since),
            "dataFineAggiornamento": _iso(now if now is not None else time.time())}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, headers=headers) as client:
            r = client.post(f"{api}/ricerca/aggiornati", json=body)
            if r.status_code != 200:
                raise TransportError(f"export: aggiornati {r.status_code}: {r.text[:200]}")
            acts = r.json().get("listaAtti") or []
    except httpx.HTTPError as e:
        raise TransportError(f"export: aggiornati: {type(e).__name__}: {e}") from e
    except ValueError as e:
        raise TransportError(f"export: aggiornati answered no json: {e}") from e
    out = set()
    for a in acts:
        try:
            out.add((str(a.get("denominazioneAtto") or ""), int(a.get("annoProvvedimento") or 0)))
        except (TypeError, ValueError):
            continue
    _feed_cache.clear()
    _feed_cache[key] = out
    return out


def partition_touched(partition: dict, updated: set[tuple[str, int]]) -> bool:
    """Does the feed name an act of this partition? A partition without a
    type is touched by any act of its year."""
    year = int(partition["year"])
    want = partition.get("type")
    return any(y == year and (not want or t == want) for t, y in updated)


def forget_feed() -> None:
    _feed_cache.clear()
