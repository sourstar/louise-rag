"""Mock embed endpoint — the only fake in the two-node flow.

Deterministic text -> unit vector, so the producer and consumer containers
genuinely agree the way two nodes sharing one model would ("recorded vectors"
as a function rather than a file). Serves the two routes the app actually
uses:

    POST /api/embed   {"model": ..., "input": [...]} -> {"embeddings": [...]}
    POST /api/show    -> {"digest": <fixed>}   (spaces.resolve_embed_model_hash,
                          which is what makes both nodes compute the SAME
                          confirmed space_id with no monkeypatching anywhere)

MOCK_SALT selects the "model": the adversarial suite's wrong-model scenarios
run in-process, but the knob is here too so the compose flow could stage one.
"""
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = int(os.getenv("MOCK_DIM", "64"))
SALT = os.getenv("MOCK_SALT", "model-A")
DIGEST = hashlib.sha256(f"mock-weights:{SALT}".encode()).hexdigest()


def embed_one(text: str) -> list[float]:
    seed = hashlib.blake2b(f"{SALT}\x00{text}".encode(), digest_size=32).digest()
    values, counter = [], 0
    while len(values) < DIM:
        block = hashlib.blake2b(seed + counter.to_bytes(4, "big"),
                                digest_size=32).digest()
        for i in range(0, len(block) - 3, 4):
            raw = int.from_bytes(block[i:i + 4], "big")
            values.append((raw / 2 ** 31) - 1.0 + 0.6)   # anisotropic: shared bias
            if len(values) == DIM:
                break
        counter += 1
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply({"ok": True, "mock": "embed", "salt": SALT})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._reply({"error": "bad json"}, 400)
            return
        if self.path == "/api/show":
            self._reply({"digest": DIGEST})
        elif self.path == "/api/embed":
            texts = body.get("input") or []
            self._reply({"embeddings": [embed_one(t) for t in texts]})
        else:
            self._reply({"error": "unknown route"}, 404)


if __name__ == "__main__":
    port = int(os.getenv("MOCK_PORT", "11434"))
    print(f"mock embed endpoint on :{port} (dim={DIM}, salt={SALT})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
