"""app/vega/service.py — the receipts service: the bounded store, the runs
under their own names, the intake on its socket, the read-only HTTP API."""
from __future__ import annotations

import json
import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.hold.codex import canonical_json
from app.rage import runs as rage_runs
from app.rage.store import RunStore
from app.vega import service
from app.vega.service import ReceiptStore, Refused, Runs

RUN = {"driver": "cpu", "weights": "sha256:" + "11" * 32,
       "engine": {"name": "localai/fake", "build": "v1", "variant": None,
                  "image": "sha256:" + "22" * 32, "args": ["/b/run.sh", "--addr",
                                                       "127.0.0.1:${PORT}"],
                  "env": {}, "device": "cpu", "options": {"ModelFile": "${WEIGHTS}"}}}
RUN_ID = rage_runs.run_id(RUN)


def receipt(i: int = 0, **kw: str) -> dict[str, str]:
    r = {"id": f"{i:032x}", "run_id": RUN_ID, "input_digest": "sha256:" + "33" * 32,
         "vector_digest": "sha256:" + "44" * 32}
    return r | kw


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_a_receipt_lives_its_time_and_no_longer():
    clock = Clock()
    store = ReceiptStore(ttl_s=600, clock=clock)
    assert store.put(receipt(1)) is True
    clock.t += 599
    assert store.get([receipt(1)["id"]]) == [receipt(1)]
    clock.t += 2
    assert store.get([receipt(1)["id"]]) == [] and len(store) == 0


def test_the_store_is_bounded_oldest_first():
    store = ReceiptStore(max_receipts=3, clock=Clock())
    for i in range(5):
        store.put(receipt(i))
    assert len(store) == 3
    assert [r["id"] for r in store.get([receipt(i)["id"] for i in range(5)])] == \
        [receipt(i)["id"] for i in (2, 3, 4)]


def test_an_id_is_one_time_the_same_receipt_again_is_not_a_second():
    store = ReceiptStore(clock=Clock())
    assert store.put(receipt(1)) is True
    assert store.put(receipt(1)) is False, "a wrapper resending after a lost answer"
    with pytest.raises(Refused, match="one-time") as e:
        store.put(receipt(1, vector_digest="sha256:" + "55" * 32))
    assert e.value.code == service.CONFLICT
    assert store.get([receipt(1)["id"]]) == [receipt(1)], "the first stands"


def test_a_run_is_taken_only_under_its_own_name(tmp_path):
    runs = Runs(RunStore(tmp_path / "runs.sqlite"))
    with pytest.raises(Refused, match="not H"):
        runs.put("sha256:" + "00" * 32, RUN)
    with pytest.raises(Refused, match="exactly driver"):
        runs.put(RUN_ID, {"driver": "cpu"})
    runs.put(RUN_ID, RUN)
    assert runs.known(RUN_ID) and runs.get(RUN_ID) == RUN
    again = Runs(RunStore(tmp_path / "runs.sqlite"))
    assert again.known(RUN_ID), "kept across a restart: a statement may come later"


@pytest.mark.parametrize("line, code", [
    (b"not json", service.INVALID),
    (b'{"kind":"receipt","kind":"run"}', service.INVALID),
    (b'{"kind":"nope"}', service.INVALID),
    (b'{"kind":"receipt","id":"x","run_id":"y","input_digest":"z","vector_digest":"w"}',
     service.INVALID),
    (b'{"kind":"run","run_id":"sha256:00","run":{},"x":1.5}', service.INVALID),
])
def test_the_intake_refuses_what_is_not_a_message(tmp_path, line, code):
    reply = service.handle(line, ReceiptStore(), Runs(RunStore(tmp_path / "r.sqlite")))
    assert reply["ok"] is False and reply["code"] == code


def test_a_receipt_for_a_run_never_told_is_refused_until_it_is(tmp_path):
    receipts, runs = ReceiptStore(), Runs(RunStore(tmp_path / "r.sqlite"))
    line = canonical_json({"kind": "receipt", **receipt(7)})
    assert service.handle(line, receipts, runs)["code"] == service.UNKNOWN_RUN
    told = canonical_json({"kind": "run", "run_id": RUN_ID, "run": RUN})
    assert service.handle(told, receipts, runs) == {"ok": True}
    assert service.handle(line, receipts, runs) == {"ok": True}
    assert receipts.get([receipt(7)["id"]]) == [receipt(7)]


def _ask(path, *messages):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(path))
        f = s.makefile("rb")
        out = []
        for m in messages:
            s.sendall(canonical_json(m) + b"\n")
            out.append(json.loads(f.readline()))
        return out


def test_the_intake_answers_on_its_socket_one_line_each(vega):
    replies = _ask(vega.socket, {"kind": "run", "run_id": RUN_ID, "run": RUN},
                   {"kind": "receipt", **receipt(3)}, {"kind": "receipt", **receipt(3)})
    assert replies == [{"ok": True}] * 3
    assert vega.receipts.get([receipt(3)["id"]]) == [receipt(3)]
    assert oct(vega.socket.stat().st_mode & 0o777) == oct(service.SOCKET_MODE)


def test_a_stale_socket_is_replaced_and_a_file_is_not(tmp_path):
    import asyncio
    from app.vega.service import Intake
    stale = tmp_path / "s.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale))
    s.close()
    intake = Intake(stale, ReceiptStore(), Runs(RunStore(tmp_path / "r.sqlite")),
                    log=lambda _m: None)

    async def cycle():
        await intake.start()
        await intake.close()
    asyncio.run(cycle())
    plain = tmp_path / "plain"
    plain.write_text("not a socket")
    other = Intake(plain, ReceiptStore(), Runs(RunStore(tmp_path / "r.sqlite")))
    with pytest.raises(RuntimeError, match="not a socket"):
        asyncio.run(other.start())
    assert plain.read_text() == "not a socket"


def test_the_http_api_reads_receipts_and_runs(tmp_path):
    receipts, runs = ReceiptStore(), Runs(RunStore(tmp_path / "r.sqlite"))
    runs.put(RUN_ID, RUN)
    receipts.put(receipt(1))
    app = FastAPI()
    app.include_router(service.router(receipts, runs))
    http = TestClient(app)
    got = http.get("/vega/receipts", params={"ids": f"{receipt(1)['id']},{receipt(2)['id']}"})
    assert got.status_code == 200 and got.json() == {"receipts": [receipt(1)]}, \
        "the missing one is simply absent"
    assert http.get(f"/vega/runs/{RUN_ID}").json() == RUN
    assert http.get("/vega/runs/sha256:" + "00" * 32).status_code == 404
    assert http.get("/vega/receipts", params={"ids": "nothex"}).status_code == 400
    assert http.get("/vega/receipts").status_code == 400
    many = ",".join(f"{i:032x}" for i in range(service.MAX_IDS + 1))
    assert http.get("/vega/receipts", params={"ids": many}).status_code == 400
    assert http.post("/vega/receipts").status_code == 405, "read-only"


# ---- the trees: a backend's per-file list under its own digest (vega.md §12) ----

TREE = {"run.sh": {"type": "file", "exec": True, "size": 10, "sha256": "ab" * 32},
        "lib": {"type": "dir"},
        "lib/libx.so.1": {"type": "link", "target": "libx.so.1.2.3"}}
IMAGE = "sha256:" + __import__("hashlib").sha256(canonical_json(TREE)).hexdigest()


def test_a_tree_is_kept_under_its_own_digest_and_read_back(tmp_path):
    """`image` must be H of the list's canonical JSON — the wrapper's
    `tree_digest` — or the list is not the one that digest names."""
    runs = Runs(RunStore(tmp_path / "runs.sqlite"))
    with pytest.raises(Refused, match="not H of the tree"):
        runs.put_tree("sha256:" + "00" * 32, TREE)
    with pytest.raises(Refused, match="non-empty object"):
        runs.put_tree(IMAGE, {})
    with pytest.raises(Refused, match="type is one of"):
        runs.put_tree(IMAGE, {"x": {"type": "socket"}})
    with pytest.raises(Refused, match="is a digest"):
        runs.put_tree("not-a-digest", TREE)
    runs.put_tree(IMAGE, TREE)
    assert runs.get_tree(IMAGE) == TREE
    assert runs.get_tree("sha256:" + "00" * 32) is None
    again = Runs(RunStore(tmp_path / "runs.sqlite"))
    assert again.get_tree(IMAGE) == TREE, "kept across a restart, like a run"
    runs.put_tree(IMAGE, TREE)
    assert again.get_tree(IMAGE) == TREE, "the same bytes again are the same row"


def test_the_intake_takes_a_tree_message_and_the_http_api_serves_it(tmp_path):
    receipts, runs = ReceiptStore(), Runs(RunStore(tmp_path / "r.sqlite"))
    assert service.handle(canonical_json({"kind": "tree", "image": IMAGE, "tree": TREE}),
                          receipts, runs) == {"ok": True}
    bad = service.handle(canonical_json({"kind": "tree", "image": IMAGE, "tree": TREE, "x": 1}),
                         receipts, runs)
    assert bad["ok"] is False and bad["code"] == service.INVALID
    wrong = service.handle(canonical_json({"kind": "tree", "image": "sha256:" + "00" * 32,
                                           "tree": TREE}), receipts, runs)
    assert wrong["ok"] is False and "not H of the tree" in wrong["error"]
    app = FastAPI()
    app.include_router(service.router(receipts, runs))
    http = TestClient(app)
    assert http.get(f"/vega/trees/{IMAGE}").json() == TREE
    assert http.get("/vega/trees/sha256:" + "00" * 32).status_code == 404
    assert http.get("/vega/trees/nope").status_code == 404
    assert service.MAX_LINE >= 16 << 20, "a tree's list is megabytes: the line limit takes it"
