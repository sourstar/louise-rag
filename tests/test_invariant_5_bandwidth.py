"""Invariant 5 (bandwidth), mechanically: no module under app/ moves bytes
over the network without drawing from the central token-bucket budget
(app/transfer, through app/acquire/budget.py's `acquire_bytes` or the
manager's own calls) — or it is on the NAMED exemption list below, each
entry with its reason.

A module "does network I/O" when its CODE (not a comment, not a string)
calls a network client: httpx's client or module-level verbs, the pooled
model client app/httpc.py, requests / urllib.request / ftplib / a raw
socket, libtorrent, or a subprocess whose argv is git / rsync / curl /
wget / ssh / scp / sftp / aria2c. A module that only names httpx's
exception classes, or that hands a URL to a transport, is not one.

A module "draws" when its code reaches the budget: `acquire_bytes`,
`note_external_bytes`, `rsync_reservation` / `rsync_lease` (the rate lease
for what cannot be metered per byte), a bucket's `acquire` / `charge` /
`note_granted`, or a transfer handle bound with `get_manager().bind(...)`.

The test fails the moment a new module starts doing network I/O without
either, and when an exemption names a module that no longer does any (a
stale entry is a hole someone else may walk through).
"""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

#: Model-server calls are not bulk transfer: they carry a model's input and
#: output, which the budget has never covered (app/httpc.py says so), and
#: metering them would put a query's encoder call behind a backfill
#: download. Every entry is a module that talks ONLY to a model server or to
#: the node's own services — never to an origin or a peer.
MODEL_CALLS = "model-server call (encoder, chat, rerank, translate): not bulk transfer"
EXEMPT: dict[str, str] = {
    "app/httpc.py": f"{MODEL_CALLS} — the pooled client every model call shares",
    "app/embeddings.py": f"{MODEL_CALLS} — the encoder",
    "app/spaces.py": f"{MODEL_CALLS} — the encoder's weights digest at boot",
    "app/retrieval.py": f"{MODEL_CALLS} — the reranker and the encoder's tokenizer",
    "app/llm.py": f"{MODEL_CALLS} — the chat model",
    "app/translate.py": f"{MODEL_CALLS} — the translation model",
    "app/model_load.py": f"{MODEL_CALLS} — the runner's /system and backend shutdown",
    "app/model_pins.py": f"{MODEL_CALLS} — LocalAI's pin API",
    "app/run_check.py": f"{MODEL_CALLS} — the run an encoder states (Louise-Run)",
    "app/vega/client.py": f"{MODEL_CALLS} — VeGa, the encoders' wrapper",
    "app/vega/trees.py": f"{MODEL_CALLS} — VeGa's image tree of an encoder's engine",
    "app/gpu.py": f"{MODEL_CALLS} — the GPU probe beside the model containers",
    "app/gpu_models.py": f"{MODEL_CALLS} — the models the runner lists",
    "app/api.py": f"{MODEL_CALLS} — `GET /v1/models` of the node's model container",
    "app/web.py": f"{MODEL_CALLS} — the System page's health pings of the model endpoints",
}

_NET_BINARIES = {"git", "rsync", "curl", "wget", "ssh", "scp", "sftp", "aria2c"}
#: client entry points per network module; an attribute not listed (an
#: exception class, a Limits, a Timeout) is not I/O
_CLIENT_CALLS = {
    "httpx": {"Client", "AsyncClient", "get", "post", "put", "patch", "delete", "head",
              "options", "request", "stream"},
    "app.httpc": {"client", "get", "post", "put"},
    "requests": {"Session", "get", "post", "put", "patch", "delete", "head", "options", "request"},
    "aiohttp": {"ClientSession", "request"},
    "urllib.request": {"urlopen", "urlretrieve", "build_opener"},
    "http.client": {"HTTPConnection", "HTTPSConnection"},
    "ftplib": {"FTP", "FTP_TLS"},
    "socket": {"socket", "create_connection", "socketpair"},
}
_ALWAYS_NETWORK = {"libtorrent", "urllib3", "websockets", "paramiko"}
_SUBPROCESS = "subprocess"

_DRAW_NAMES = {"acquire_bytes", "note_external_bytes", "rsync_reservation", "rsync_lease"}
_BUCKET_DRAWS = {"acquire", "charge", "note_granted"}


def _module_name(path: Path) -> str:
    rel = path.relative_to(APP.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve(node: ast.ImportFrom, here: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    base = here.split(".")
    drop = node.level - (1 if is_package else 0)
    base = base[:len(base) - drop] if drop else base
    return ".".join(base + ([node.module] if node.module else []))


def _aliases(tree: ast.AST, here: str, is_package: bool) -> tuple[dict[str, str], set[str]]:
    """Local name -> the dotted thing it is bound to, for every import in
    the file (module level or inside a function: the lazy imports are the
    common case here). And the set of modules imported at all."""
    names: dict[str, str] = {}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name)
                names[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            mod = _resolve(node, here, is_package)
            imported.add(mod)
            for a in node.names:
                full = f"{mod}.{a.name}" if mod else a.name
                imported.add(full)
                names[a.asname or a.name] = full
    return names, imported


def _dotted(node: ast.AST, names: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return names.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, names)
        return f"{base}.{node.attr}" if base else None
    return None


def network_io(source: str, here: str = "app.x", is_package: bool = False) -> set[str]:
    """What network I/O the code does, as a set of short labels; empty for
    none."""
    tree = ast.parse(source)
    names, imported = _aliases(tree, here, is_package)
    found: set[str] = {m for m in imported if m.split(".")[0] in _ALWAYS_NETWORK}
    uses_subprocess = any(m == _SUBPROCESS or m.startswith(_SUBPROCESS + ".") for m in imported)
    for node in ast.walk(tree):
        # a call, or a reference handed on (`get=httpx.get`): the module
        # chose the client either way
        if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load):
            target = _dotted(node, names)
            if target:
                mod, _, attr = target.rpartition(".")
                if attr in _CLIENT_CALLS.get(mod, ()):
                    found.add(target)
        if uses_subprocess and isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value in _NET_BINARIES:
                found.add(f"subprocess {first.value}")
    return found


def draws_from_budget(source: str) -> bool:
    tree = ast.parse(source)
    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                 {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    if referenced & _DRAW_NAMES:
        return True
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        recv = node.func.value
        # get_manager().bucket(Direction.X).acquire / charge / note_granted
        if node.func.attr in _BUCKET_DRAWS and isinstance(recv, ast.Call) \
                and isinstance(recv.func, ast.Attribute) and recv.func.attr == "bucket":
            return True
        # get_manager().bind(...) / manager.bind(...): a transfer handle
        if node.func.attr == "bind" and "get_manager" in referenced:
            return True
    return False


def _scan() -> dict[str, tuple[set[str], bool]]:
    out = {}
    for p in sorted(APP.rglob("*.py")):
        src = p.read_text(encoding="utf-8")
        io = network_io(src, _module_name(p), p.name == "__init__.py")
        if io:
            out[p.relative_to(APP.parent).as_posix()] = (io, draws_from_budget(src))
    return out


def test_every_module_doing_network_io_draws_from_the_budget_or_is_exempt_by_name():
    scan = _scan()
    assert len(scan) >= 10, f"the scan found too little ({sorted(scan)}) — the detector is wrong, not app/"
    offenders = sorted(f"{m}: {sorted(io)}" for m, (io, draws) in scan.items()
                       if not draws and m not in EXEMPT)
    assert offenders == [], (
        "invariant 5: these modules move bytes over the network without drawing from the "
        "central budget (app/acquire/budget.py acquire_bytes, or the transfer manager), and "
        "are not on the named exemption list in this test:\n  " + "\n  ".join(offenders))


def test_every_exemption_still_names_a_module_doing_network_io_and_not_drawing():
    scan = _scan()
    stale = sorted(m for m in EXEMPT if m not in scan)
    assert stale == [], f"exemptions naming a module that does no network I/O any more: {stale}"
    drawing = sorted(m for m in EXEMPT if scan[m][1])
    assert drawing == [], f"exemptions naming a module that now draws from the budget: {drawing}"
    unreasoned = sorted(m for m, why in EXEMPT.items() if len(why.strip()) < 20)
    assert unreasoned == [], f"an exemption carries its reason: {unreasoned}"


def test_the_transports_and_peer_paths_are_seen_and_draw():
    """The detector sees the modules the invariant is about, and each is
    counted as drawing — a guard that saw none of them would pass forever."""
    scan = _scan()
    for m in ("app/acquire/transport_https.py", "app/acquire/transport_ftp.py",
              "app/acquire/transport_rsync.py", "app/acquire/transport_torrent.py",
              "app/acquire/transport_hf_hub.py", "app/hold/sources.py",
              "app/share/gossip.py", "app/hold/anchor.py"):
        assert m in scan, f"{m} is not seen doing network I/O — the detector is wrong"
        assert scan[m][1], f"{m} is not seen drawing from the budget"


def test_the_detector_sees_calls_not_comments_and_not_exception_classes():
    assert network_io("# httpx.get(url)\n'httpx.post(url)'\n") == set()
    assert network_io("import httpx\ntry:\n    pass\nexcept httpx.TransportError:\n    pass\n") == set()
    assert network_io("import httpx\nhttpx.get('http://x')\n") == {"httpx.get"}
    assert network_io("def f():\n    import httpx\n    with httpx.Client() as c:\n        c.get('u')\n") \
        == {"httpx.Client"}
    assert network_io("from . import httpc\nhttpc.post('u')\n", "app.embeddings") == {"app.httpc.post"}
    assert network_io("from .. import httpc\nhttpc.get('u')\n", "app.vega.client") == {"app.httpc.get"}
    assert network_io("import subprocess\nsubprocess.run(['git', 'clone', u])\n") == {"subprocess git"}
    assert network_io("import subprocess\nsubprocess.run(['cp', '-a', a, b])\n") == set()
    assert network_io("x = ['rsync', '-a']\n") == set(), "no subprocess, no rsync run"
    assert network_io("def f():\n    import libtorrent as lt\n") == {"libtorrent"}
    assert network_io("import httpx\nf(url, get=httpx.get)\n") == {"httpx.get"}, "a client handed on"
    assert network_io("from httpx import Client\nClient()\n") == {"httpx.Client"}
    assert network_io("import socket\nsocket.gethostname()\n") == set()
    assert network_io("import socket\nsocket.create_connection(('h', 1))\n") == {"socket.create_connection"}


def test_the_detector_sees_each_way_of_drawing():
    assert draws_from_budget("from .acquire import budget\nbudget.acquire_bytes(5)\n")
    assert draws_from_budget("f(charge=budget.acquire_bytes)\n")
    assert draws_from_budget("get_manager().bucket(Direction.DOWN).acquire(n, Priority.DELTA)\n")
    assert draws_from_budget("get_manager().bucket(Direction.UP).charge(n, p)\n")
    assert draws_from_budget("m = get_manager()\nwith m.bind(Priority.SEEDING, Direction.UP):\n    pass\n")
    assert draws_from_budget("with get_manager().rsync_lease(None) as bps:\n    pass\n")
    assert not draws_from_budget("get_manager().rates()\n")
    assert not draws_from_budget("# budget.acquire_bytes(5)\nx = 'acquire_bytes'\n")
