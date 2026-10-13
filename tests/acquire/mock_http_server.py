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

    def do_HEAD(self):
        """The file's length and no body — what `HttpsTransport.size_of`
        asks. Counted apart from GETs, so `request_count` still means "the
        file's bytes were served"."""
        server: "MockServer" = self.server.mock   # type: ignore[attr-defined]
        name = self.path.lstrip("/")
        path = server.root / name
        with server.lock:
            server.head_counts[name] = server.head_counts.get(name, 0) + 1
        if name in server.no_head or not path.is_file():
            self.send_response(405 if name in server.no_head else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def _serve(self, server: "MockServer", name: str, path: Path):
        with server.lock:
            failing = server.failures.get(name, 0)
            if failing:
                server.failures[name] = failing - 1
        range_now = self.headers.get("Range") or ""
        open_ended = range_now.startswith("bytes=") and range_now.endswith("-")
        if failing or (open_ended and name in server.refused_open_ranges):
            # a transient fault at the origin: the connection closes with no
            # answer at all, what a client sees as a dropped connection or,
            # had it waited, a read timeout. `refused_open_ranges` models the
            # origin seen 2026-10-13: a resume `bytes=N-` stalls, a closed
            # range at the same offset answers
            self.close_connection = True
            return
        target = server.redirects.get(name)
        if target is not None:
            # 302 like Hugging Face's /resolve/ -> CDN hop; Location is
            # same-server so the test exercises the client's redirect
            # following, not cross-host semantics
            self.send_response(302)
            self.send_header("Location", f"{server.base_url}/{target}")
            self.send_header("Content-Length", "0")   # keep-alive needs it
            self.end_headers()
            return
        if not path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        data = path.read_bytes()
        total = len(data)
        start = 0
        end = total - 1
        range_header = self.headers.get("Range")
        # `no_ranges` models an origin that ignores Range entirely and answers
        # 200 with the whole file — legal HTTP, and fatal to a segmented
        # download, so the client has to detect it and fall back.
        if range_header and range_header.startswith("bytes=") and name not in server.no_ranges:
            spec = range_header.split("=", 1)[1]
            raw_start, _, raw_end = spec.partition("-")
            start = int(raw_start or 0)
            # A CLOSED range ("bytes=0-999") is a partial request even when it
            # starts at zero; only an open-ended one from zero is the whole
            # file. Segmented downloads send closed ranges for every slice.
            if raw_end:
                end = min(int(raw_end), total - 1)
            server.last_range_header[name] = range_header
            with server.lock:
                server.range_requests.setdefault(name, []).append(range_header)
        else:
            range_header = None
        remaining = data[start:end + 1]

        partial = range_header is not None and (start > 0 or end < total - 1)
        if partial:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
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

        cut = server.cuts.get(name)        # bytes per RESPONSE, whatever its range
        if cut is not None and len(remaining) > cut:
            try:
                self.wfile.write(remaining[:cut])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True   # the origin's limit on one response, 2026-10-13
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
        self.range_requests: dict[str, list[str]] = {}
        self.no_ranges: set[str] = set()     # names served without Range support
        self.last_user_agent: dict[str, str | None] = {}
        self.redirects: dict[str, str] = {}
        self.head_counts: dict[str, int] = {}
        self.no_head: set[str] = set()       # names whose HEAD is refused (405)
        self.failures: dict[str, int] = {}   # names whose next N GETs drop the connection
        self.cuts: dict[str, int] = {}       # names whose every response body ends after N bytes
        self.refused_open_ranges: set[str] = set()   # names whose `bytes=N-` requests get no answer
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

    def fail_next(self, name: str, n: int) -> None:
        """The next `n` GETs of `name` drop the connection without an answer —
        a transient fault, gone by itself, which a client must retry rather
        than give up on."""
        with self.lock:
            self.failures[name] = n

    def cut_after_bytes(self, name: str, n: int) -> None:
        """Every response for `name` carries at most `n` bytes of body, then
        the connection closes — an origin's limit on one response (seen
        2026-10-13: 1,086,390,272 bytes of a 1.7 GB zip, every time). A
        client finishes the file only in requests smaller than that."""
        self.cuts[name] = n

    def refuse_open_ranges(self, name: str) -> None:
        """A resume `Range: bytes=N-` for `name` gets no answer at all, while a
        closed range `bytes=N-M` is served — the origin's second trait of
        2026-10-13."""
        self.refused_open_ranges.add(name)

    def request_count(self, name: str) -> int:
        return self.request_counts.get(name, 0)

    def head_count(self, name: str) -> int:
        return self.head_counts.get(name, 0)

    def last_range(self, name: str) -> str | None:
        return self.last_range_header.get(name)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
