"""hold.source — a deposit collection's URL sources (06 §6.7, 04 §4.4;
migration step 16e).

LocalRecall's behaviour (`rag/source_manager.go`, `rag/sources/` at
4cae67b), which LocalAGI embeds, reproduced so its clients cannot tell the
difference. A source is a SUBSCRIPTION INSIDE THE COLLECTION: a URL and an
interval in minutes, kept in the collection's file (desired state,
invariant 10); ONE deposit per URL, keyed `source-<collection>-<url>.txt`,
replaced whole each time it is fetched (a page that comes back as the
same bytes is left as it is); a fetch that fails or comes back empty
keeps the deposit it had. What a URL gives:

    …ends .git          the repository shallow-cloned at HEAD, its text
                        files concatenated, each under a `--- File: … ---` line
    …ends sitemap.xml   every page the sitemap lists, fetched, texts joined
    anything else       the one page, KEPT AS FETCHED: its bytes are the
                        deposit and its record is parsed from them — its
                        HTML as text, and what its head says about it
                        (06 §6.7, amended 2026-10-25, operator: "Keep all")

The page's body is kept, not the HTTP response around it. The standard
form for that is WARC (ISO 28500): the request, the response headers and
the redirect chain beside the payload. Adopting it takes a WARC writer
(`warcio`, a new dependency) and an idempotence key on the payload digest
rather than the file's hash, since a WARC record carries its fetch time;
it pays once a header (Last-Modified, Content-Type's charset) or the
redirect is wanted in the record.

Three additions, none of which LocalAGI can see: every byte draws from the
transfer budget (invariant 5); nothing is fetched before 04 §4.4's rule
holds (`licensing.web_source_refusal`); the SSH key is a path
(`GIT_PRIVATE_KEY_FILE`, invariant 8). And one departure: a source is
fetched when it is ADDED — LocalRecall stores a new source without putting
it on its running ticker, so it is first fetched at the next restart.

Everything here that writes runs in the worker (app/jobs.py DEPOSIT, and
`serve`, the worker's sources thread).
"""
from __future__ import annotations

import calendar
import hashlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from contextlib import closing
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from app.plugins.deposit import html as page_html

from . import deposit

#: LocalRecall's source ticker: every minute.
TICK_S = 60.0
#: LocalRecall's page timeout; ours is per request, and a clone has its own.
PAGE_TIMEOUT_S = 30.0
CLONE_TIMEOUT_S = 600.0
#: A page larger than this is refused rather than read into memory whole.
MAX_PAGE_BYTES = 50 * 2**20
#: A clone's bound in bytes, as MAX_PAGE_BYTES is a page's: what lands under
#: `.git` (the pack the network delivered) past this stops the clone.
MAX_CLONE_BYTES = 2 * 2**30
#: How often a running clone's landed bytes are measured and drawn.
CLONE_POLL_S = 0.5
DEFAULT_INTERVAL_MIN = 60

_USER_AGENT = "louise-rag/0.1 (+https://github.com/sourstar/louise-rag)"

#: LocalRecall's `isTextFile`, by extension, exactly (rag/sources/git.go).
TEXT_EXTENSIONS = frozenset((
    ".txt", ".md", ".go", ".py", ".js", ".ts", ".html", ".css", ".json", ".yaml",
    ".yml", ".xml", ".sh", ".bash", ".c", ".cpp", ".h", ".hpp", ".java", ".rb",
    ".php", ".rs", ".swift", ".kt", ".scala", ".sql", ".proto", ".toml", ".ini",
    ".conf", ".log", ".csv", ".tsv", ".rst", ".tex", ".adoc", ".asciidoc", ".wiki"))

#: `sanitizeURL`'s replacer, in its order: Go's strings.Replacer tries the
#: old strings in argument order at each position, so `://` wins over `:`.
_REPLACED = ("://", "/", "?", "&", "=", "#", "@", ":", ".", "+", " ")


class SourceError(deposit.DepositError):
    """A source refused, or a fetch that brought nothing back."""


# --------------------------------------------------------------------------
# names

def sanitize_url(url: str) -> str:
    """LocalRecall's `sanitizeURL`, byte for byte: lower-cased, each of
    `://` `/` `?` `&` `=` `#` `@` `:` `.` `+` and space to `-`, runs of `-`
    collapsed, `-` trimmed from both ends, cut at 255 bytes."""
    s, out, i = url.lower(), [], 0
    while i < len(s):
        for old in _REPLACED:
            if s.startswith(old, i):
                out.append("-")
                i += len(old)
                break
        else:
            out.append(s[i])
            i += 1
    text = "".join(out)
    while "--" in text:
        text = text.replace("--", "-")
    text = text.strip("-")
    raw = text.encode("utf-8")
    return raw[:255].decode("utf-8", "ignore") if len(raw) > 255 else text


def source_key(collection: str, url: str) -> str:
    """`source-<collection>-<sanitised url>.txt`, LocalRecall's entry name.
    Cut, where it must be, to 02 §2.9's 255 bytes — LocalRecall's own file
    name would not fit a filesystem past that either."""
    head, tail = f"source-{collection}-", ".txt"
    room = 255 - len(head.encode()) - len(tail.encode())
    s = sanitize_url(url).encode("utf-8")[:room].decode("utf-8", "ignore")
    return head + s + tail


# --------------------------------------------------------------------------
# the subscriptions, in the collection's file

def _held_file(name: str) -> Path:
    from app import collections as collections_mod
    return deposit.held_file(collections_mod.louise_root() / "collections", name)


def subscriptions(name: str) -> list[dict[str, Any]]:
    """`[[sources]]` of the collection's file: `{url, interval}` each."""
    try:
        with open(_held_file(name), "rb") as f:
            raw = tomllib.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for s in raw.get("sources") or []:
        if isinstance(s, dict) and isinstance(s.get("url"), str):
            out.append({"url": s["url"],
                        "interval": int(s.get("interval") or DEFAULT_INTERVAL_MIN)})
    return out


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_subscriptions(name: str, subs: list[dict[str, Any]]) -> None:
    """The file rewritten with these `[[sources]]` tables — kept LAST, so the
    rest of the file is carried through as written."""
    p = _held_file(name)
    text = p.read_text(encoding="utf-8")
    m = re.search(r"(?m)^\[\[sources\]\]\s*$", text)
    head = (text[:m.start()] if m else text).rstrip("\n") + "\n"
    for s in subs:
        head += f"\n[[sources]]\nurl = {_toml_str(s['url'])}\ninterval = {int(s['interval'])}\n"
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_text(head, encoding="utf-8")
    tomllib.loads(head)                        # never leave a file TOML cannot read
    tmp.replace(p)


def _last_key(url: str) -> str:
    return "source_last:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _collection(name: str):
    return deposit._collection(name)


def last_update(name: str, url: str) -> str:
    """When the source was last fetched with something to keep — runtime
    state, in the manifest, never desired state (invariant 10). "" before
    its first fetch."""
    from app import db
    c = _collection(name)
    with closing(deposit._connect(c, write=False)) as con:
        return db.get_control(con, _last_key(url)) or ""


def listing(name: str) -> list[dict[str, Any]]:
    """`hold source list`, LocalAGI's `ListSources`: url, update_interval
    (minutes), last_update (RFC 3339, "" before the first fetch)."""
    return [{"url": s["url"], "update_interval": s["interval"],
             "last_update": last_update(name, s["url"])} for s in subscriptions(name)]


# --------------------------------------------------------------------------
# add and erase — the worker's (app/jobs.py DEPOSIT)

_wake = threading.Event()


def add(name: str, url: str, interval: int = DEFAULT_INTERVAL_MIN) -> dict[str, Any]:
    """`hold source add`: the subscription recorded, refused before that if
    04 §4.4's rule does not hold. The fetch is the sources thread's, which
    is woken to do it now rather than at the next tick."""
    from app import licensing
    _collection(name)
    url = (url or "").strip()
    if not url:
        raise SourceError("a source is a URL")
    refusal = licensing.web_source_refusal(url)
    if refusal:
        raise SourceError(refusal)
    interval = max(1, int(interval or DEFAULT_INTERVAL_MIN))
    subs = subscriptions(name)
    if any(s["url"] == url for s in subs):
        raise SourceError(f"source {url} already exists")   # LocalRecall's answer
    subs.append({"url": url, "interval": interval})
    _write_subscriptions(name, subs)
    _wake.set()
    return {"url": url, "update_interval": interval, "key": source_key(name, url)}


def erase(name: str, url: str) -> dict[str, Any]:
    """`hold source erase`: the subscription dropped and its deposit erased,
    leaving no trace (02 §2.9). An entry never fetched is not an error."""
    from app import db
    c = _collection(name)
    subs = subscriptions(name)
    if not any(s["url"] == url for s in subs):
        raise SourceError(f"source {url} not found")
    _write_subscriptions(name, [s for s in subs if s["url"] != url])
    key = source_key(name, url)
    if key in deposit.keys(name):
        deposit.erase(name, key)
    with closing(deposit._connect(c)) as con:
        con.execute("DELETE FROM control WHERE key=?", (_last_key(url),))
    return {"url": url, "erased": key}


# --------------------------------------------------------------------------
# fetching

#: A page's text, and what it says about itself, are the deposit plugin's:
#: the record is parsed from the page's bytes (app/plugins/deposit/html.py).
html_to_text = page_html.to_text


def _get(url: str) -> bytes:
    """One GET, every chunk charged to the transfer budget after it lands
    (invariant 5; app/acquire/budget.py's pay-after rule), bounded in time
    and in size."""
    import httpx
    from app.acquire import budget
    out = bytearray()
    with httpx.Client(timeout=PAGE_TIMEOUT_S, follow_redirects=True,
                      headers={"User-Agent": _USER_AGENT}) as client:
        with client.stream("GET", url) as r:
            if r.status_code != 200:
                raise SourceError(f"{url}: HTTP {r.status_code}")
            for chunk in r.iter_bytes(64 * 1024):
                out += chunk
                budget.acquire_bytes(len(chunk))
                if len(out) > MAX_PAGE_BYTES:
                    raise SourceError(f"{url}: more than {MAX_PAGE_BYTES} bytes")
    return bytes(out)


def fetch_page(url: str) -> str:
    """A sitemap's page as text, exactly as before 2026-10-25."""
    return html_to_text(page_html.decode(_get(url)))


def fetch_sitemap(url: str) -> str:
    """Every `<url><loc>` of the sitemap, fetched and joined; a page that
    fails is left out, as LocalRecall leaves it out."""
    root = ElementTree.fromstring(_get(url))
    locs = [el.text.strip() for el in root.iter()
            if el.tag.rsplit("}", 1)[-1] == "loc" and el.text and el.text.strip()]
    texts = []
    for loc in locs:
        try:
            texts.append(fetch_page(loc))
        except Exception:                                   # noqa: BLE001
            continue
    return "\n".join(texts)


def fetch_git(url: str) -> str:
    """A shallow clone at HEAD, its text files concatenated in LocalRecall's
    shape. The clone draws from the budget AS IT LANDS (invariant 5): git
    takes no rate cap, so its process group is measured every
    CLONE_POLL_S — the bytes under `.git`, which is where the network's pack
    lands; the checkout is local — and is stopped (SIGSTOP) while each
    increment is drawn, so an exhausted budget pauses the transfer itself.
    The page's pay-after rule (`_get`), at the granularity a subprocess
    allows; bounded by CLONE_TIMEOUT_S and MAX_CLONE_BYTES."""
    import os
    from app import config
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if config.GIT_PRIVATE_KEY_FILE:
        env["GIT_SSH_COMMAND"] = ("ssh -i " + _shell_quote(config.GIT_PRIVATE_KEY_FILE)
                                  + " -o IdentitiesOnly=yes -o BatchMode=yes"
                                  " -o StrictHostKeyChecking=accept-new")
    with tempfile.TemporaryDirectory(prefix="louise-source-") as tmp:
        dest = Path(tmp) / "repo"
        _metered_clone(["git", "clone", "--depth", "1", "--single-branch",
                        "--no-tags", "--quiet", url, str(dest)], dest, url, env)
        parts = []
        for f in sorted(dest.rglob("*")):
            rel = f.relative_to(dest)
            if ".git" in rel.parts or not f.is_file():
                continue
            if f.suffix.lower() in TEXT_EXTENSIONS:
                parts.append(f"\n--- File: {rel.as_posix()} ---\n"
                             + f.read_bytes().decode("utf-8", "replace") + "\n")
        return "".join(parts)


def _landed(git_dir: Path) -> int:
    """Bytes under `git_dir` now; a file renamed or removed mid-walk (git's
    `tmp_pack_*` becoming `pack-*`) is skipped, and the next poll sees it."""
    import os
    total = 0
    for root, _dirs, files in os.walk(git_dir):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _metered_clone(argv: list[str], dest: Path, url: str, env: dict) -> None:
    import os
    import signal
    from app.acquire import budget
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            env=env, start_new_session=True)

    def group(sig) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    drawn = 0
    deadline = time.monotonic() + CLONE_TIMEOUT_S
    try:
        while True:
            try:
                proc.wait(timeout=CLONE_POLL_S)
                finished = True
            except subprocess.TimeoutExpired:
                finished = False
            landed = _landed(dest / ".git")
            if landed > MAX_CLONE_BYTES:
                raise SourceError(f"{url}: more than {MAX_CLONE_BYTES} bytes")
            if landed > drawn:
                if not finished:
                    group(signal.SIGSTOP)        # paused while the bytes are drawn
                try:
                    budget.acquire_bytes(landed - drawn)
                finally:
                    if not finished:
                        group(signal.SIGCONT)
                drawn = landed
            if finished:
                break
            if time.monotonic() > deadline:
                raise SourceError(f"{url}: clone did not finish in {CLONE_TIMEOUT_S:.0f}s")
    except BaseException:
        group(signal.SIGCONT)
        group(signal.SIGKILL)
        proc.wait()
        raise
    finally:
        err = proc.stderr.read() if proc.stderr else b""
        if proc.stderr:
            proc.stderr.close()
    if proc.returncode != 0:
        why = err.decode("utf-8", "replace").strip().splitlines()
        raise SourceError(f"{url}: git clone failed: {why[-1] if why else ''}")


def _shell_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _is_page(url: str) -> bool:
    return not (url.endswith(".git") or url.endswith("sitemap.xml"))


def fetch(url: str) -> str:
    """What the URL gives, as text, by LocalRecall's rule on its ending."""
    if url.endswith(".git"):
        return fetch_git(url)
    if url.endswith("sitemap.xml"):
        return fetch_sitemap(url)
    return fetch_page(url)


# --------------------------------------------------------------------------
# refreshing — the worker's sources thread

def refresh(name: str, url: str, *, now: float | None = None) -> bool:
    """Fetch `url` and replace its deposit whole; True if there was
    something to keep. Refused, failed or empty: the deposit it had stays
    (LocalRecall's rule), and the next tick tries again."""
    from app import db, licensing
    refusal = licensing.web_source_refusal(url)
    if refusal:
        return False
    key = source_key(name, url)
    if _is_page(url):
        # 06 §6.7, amended 2026-10-25 (operator: "Keep all"): the page is
        # kept as fetched and its record parsed from it; the same bytes
        # again change nothing. Fetched by `_get`, so the budget, the size
        # and time bounds and the gate above hold for it as for any page.
        page = _get(url)
        if not page_html.page_text(page).strip():
            return False
        deposit.add(name, key, page, url=url, now=now,
                    form=deposit._source().FORM_HTML, if_changed=True)
    else:
        text = fetch(url)
        if not text.strip():
            return False
        deposit.add(name, key, text.encode("utf-8"), url=url, now=now)
    c = _collection(name)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                          time.gmtime(now if now is not None else time.time()))
    with closing(deposit._connect(c)) as con:
        db.set_control(con, _last_key(url), stamp)
    return True


def due(name: str, now: float) -> list[str]:
    """The URLs whose interval has passed since their last fetch — or that
    were never fetched, which is every new one."""
    out = []
    for s in subscriptions(name):
        last = last_update(name, s["url"])
        if not last:
            out.append(s["url"])
            continue
        t = calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
        if now - t >= s["interval"] * 60:
            out.append(s["url"])
    return out


def tick(now: float | None = None, log=print) -> int:
    """One pass over every deposit collection's due sources; how many were
    refreshed. A source's failure is logged and never stops the others."""
    from app import collections as collections_mod, node_state
    # A URL fetched is a DOWNLOAD and its text is PARSED: a node the operator
    # has paused, or set to seeding or downloading only, fetches nothing
    # here — its due sources wait (07 §7.8's modes). An agent's own writes
    # are not this: they run in every mode (app/jobs.py DEPOSIT).
    mode = node_state.worker_state()
    if not (node_state.runs(mode, node_state.FETCH)
            and node_state.runs(mode, node_state.PARSE)):
        return 0
    now = time.time() if now is None else now
    n = 0
    for c in collections_mod.load_all():
        if c.codex is None or not deposit.is_deposit(c.codex):
            continue
        for url in due(c.name, now):
            try:
                if refresh(c.name, url, now=now):
                    n += 1
            except Exception as e:                          # noqa: BLE001
                log(f"[warn:sources] {c.name}: {url}: {type(e).__name__}: {e}")
    return n


def serve(stop, tick_s: float = TICK_S, log=print) -> None:
    """The worker's sources thread: a tick every minute, and at once when a
    source is added. Its own thread, because a sitemap is many pages and a
    clone can take minutes — never the deposit thread an agent's write
    waits on."""
    while not stop.is_set():
        try:
            tick(log=log)
        except Exception as e:                              # noqa: BLE001
            log(f"[warn:sources] {type(e).__name__}: {e}")
        deadline = time.monotonic() + tick_s
        while not stop.is_set() and time.monotonic() < deadline:
            if _wake.wait(min(1.0, tick_s)):   # an added source: now, not in a minute
                break
        _wake.clear()
