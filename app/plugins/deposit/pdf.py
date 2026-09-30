"""PDF text for the deposit plugin (06 §6.7): PDFium, through `pypdfium2`,
pinned in requirements.txt because the extraction must give the same text
every time — a re-embed has to cut the same chunks (invariant 3).

The extraction runs in a CHILD PROCESS with a deadline. A pathological PDF
can keep PDFium busy for as long as it likes, and a thread cannot be
stopped; a process can. It is a plain subprocess rather than
`multiprocessing`, because the worker parses in pool children
(app/parallel.py) and a pool child may not start children of its own.
Nothing waits on it without a bound (CLAUDE.md).

    python -m app.plugins.deposit.pdf  < file.pdf  > {"pages": [...], "meta": {...}}
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

#: The wall-clock cap on one extraction, the child's start included.
TIMEOUT_S = 60.0

_ROOT = Path(__file__).resolve().parents[3]


class PdfError(ValueError):
    """The PDF could not be read, or yielded nothing in time."""


def _extract_here(data: bytes) -> dict:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(data)
    try:
        meta = {k: (v or "") for k, v in doc.get_metadata_dict().items()}
        pages = []
        for page in doc:
            tp = page.get_textpage()
            pages.append(tp.get_text_bounded())
            tp.close()
            page.close()
        return {"pages": pages, "meta": meta}
    finally:
        doc.close()


def extract(data: bytes, timeout: float | None = None) -> dict:
    """`{"pages": [raw text per page], "meta": {Title, Author, …}}`."""
    timeout = TIMEOUT_S if timeout is None else timeout
    try:
        # From the directory that holds `app/`, whatever the caller's is:
        # `-m` resolves the module against the working directory.
        done = subprocess.run([sys.executable, "-m", __name__], input=data,
                              capture_output=True, timeout=timeout, check=False,
                              cwd=_ROOT)
    except subprocess.TimeoutExpired as e:
        raise PdfError(f"no text within {timeout:.0f}s; the PDF is refused, "
                       f"not stored (06 §6.7)") from e
    if done.returncode != 0:
        why = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise PdfError(f"not a PDF PDFium can read: {why[-1] if why else 'no reason'}")
    return json.loads(done.stdout)


def _main() -> int:
    try:
        out = _extract_here(sys.stdin.buffer.read())
    except Exception as e:     # noqa: BLE001 — the parent reports it
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
