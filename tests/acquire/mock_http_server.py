"""A minimal, deterministic mock HTTP server for acquisition tests — Range
resume support (stdlib http.server has none built in) and a
stall_after_bytes() control that simulates "kill -TERM mid-download"
deterministically, without real subprocess signaling: it declares the true
Content-Length, writes only a prefix, then closes the connection — so the
client sees a genuine truncated-transfer error, not a clean short response.

P0.7 additions, both needed to test the transfer manager against something
other than loopback speed:
  - drip(name, bps): serve a file at a bounded rate, so a transfer is slow for
    a reason the test controls.
  - live/peak concurrency counters: ThreadingHTTPServer genuinely serves
    several requests at once, so "per-endpoint concurrency never exceeded N"
    is a real assertion rather than a restatement of the client's own limit.
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_DRIP_SLICE = 4096


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass   # keep test output quiet

    def do_GET(self):
        server: "MockServer" = self.server.mock   # type: ignore[attr-defined]
        name = self.path.lstrip("/")
        path = server.root / name
        with server.lock:
            server.request_counts[name] = server.request_counts.get(name, 0) + 1
            server.last_user_agent[name] = self.headers.get("User-Agent")
            server.live += 1
            server.peak_concurrency = max(server.peak_concurrency, server.live)
        try:
            self._serve(server, name, path)
        finally:
            with server.lock:
                server.live -= 1

    def _serve(self, server: "MockServer", name: str, path: Path):
        if not path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        data = path.read_bytes()
        total = len(data)
        start = 0
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            server.last_range_header[name] = range_header
        remaining = data[start:]

        if range_header and start > 0:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(remaining)))  # the TRUE promised length
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        stall_at = server.stall.get(name)   # absolute offset in the full file
        if stall_at is not None and stall_at < total:
            send_now = remaining[: max(0, stall_at - start)]
            try:
                self.wfile.write(send_now)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True   # abrupt end: client sees fewer bytes than promised
            return

        rate = server.drip_rates.get(name)
        try:
            if rate:
                # Pace BEFORE each slice after the first, never after the last:
                # a trailing sleep would keep the handler (and therefore the
                # live-concurrency counter) alive for a slice's worth of time
                # after the client had already finished, making peak
                # concurrency read one higher than the client ever reached.
                for offset in range(0, len(remaining), _DRIP_SLICE):
                    chunk = remaining[offset:offset + _DRIP_SLICE]
                    if offset:
                        time.sleep(len(chunk) / rate)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                self.wfile.write(remaining)
        except (BrokenPipeError, ConnectionResetError):
            pass


class MockServer:
    def __init__(self, root: Path):
        self.root = root
        self.stall: dict[str, int] = {}
        self.drip_rates: dict[str, float] = {}
        self.request_counts: dict[str, int] = {}
        self.last_range_header: dict[str, str] = {}
        self.last_user_agent: dict[str, str | None] = {}
        self.lock = threading.Lock()
        self.live = 0
        self.peak_concurrency = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.mock = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def drip(self, name: str, bytes_per_second: float) -> None:
        """Serve `name` at a bounded rate, so slowness is under test control
        rather than being whatever loopback happens to do."""
        self.drip_rates[name] = bytes_per_second

    def undrip(self, name: str) -> None:
        self.drip_rates.pop(name, None)

    @property
    def base_url(self) -> str:
        _, port = self.httpd.server_address
        return f"http://127.0.0.1:{port}"

    def stall_after_bytes(self, name: str, n: int) -> None:
        self.stall[name] = n

    def unstall(self, name: str) -> None:
        self.stall.pop(name, None)

    def request_count(self, name: str) -> int:
        return self.request_counts.get(name, 0)

    def last_range(self, name: str) -> str | None:
        return self.last_range_header.get(name)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
