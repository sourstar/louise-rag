"""What a cast to float16 does to a model's weights, read from its safetensors
before the conversion (nemotron-embed.md, step 2).

    python -m tools.measure.f16_range /path/to/model.safetensors [more.safetensors …]

A bf16 checkpoint converted to an f16 GGUF (`convert_hf_to_gguf.py --outtype
f16`) is cast value by value. bf16 reaches 3.4e38 and f16 only 65,504, so a
value past that becomes inf, and one under 2^-24 in magnitude becomes 0. The
converter says nothing of either. A model with such values is not the model
in f16, however its cosines read. This counts them per tensor, without torch:
the safetensors header is JSON, bf16 is the top half of an f32.

Exit 0 when nothing overflows (underflow to zero is reported, and judged by
its count); 1 when anything would become inf.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

F16_MAX = 65504.0
#: the smallest f16 subnormal; anything smaller in magnitude becomes 0
F16_TINY = 2.0 ** -24
#: below this an f16 is subnormal: fewer than 11 significant bits
F16_NORMAL = 2.0 ** -14


def _values(raw: bytes, dtype: str):
    import numpy as np
    if dtype == "BF16":
        return (np.frombuffer(raw, dtype="<u2").astype("<u4") << 16).view("<f4")
    if dtype == "F32":
        return np.frombuffer(raw, dtype="<f4")
    if dtype == "F16":
        return np.frombuffer(raw, dtype="<f2").astype("<f4")
    return None


def scan(path: Path) -> list[dict]:
    """One row per floating tensor: its count, its largest magnitude, how many
    values overflow f16, become zero, or land in f16's subnormal range."""
    import numpy as np
    rows = []
    with path.open("rb") as fh:
        (n,) = struct.unpack("<Q", fh.read(8))
        header = json.loads(fh.read(n))
        base = 8 + n
        for name, meta in sorted(header.items()):
            if name == "__metadata__":
                continue
            start, end = meta["data_offsets"]
            fh.seek(base + start)
            v = _values(fh.read(end - start), meta["dtype"])
            if v is None:
                continue
            a = np.abs(v[np.isfinite(v)])
            rows.append({"tensor": name, "dtype": meta["dtype"], "values": int(v.size),
                         "max_abs": float(a.max()) if a.size else 0.0,
                         "non_finite": int(v.size - a.size),
                         "overflow": int((a > F16_MAX).sum()),
                         "to_zero": int(((a > 0) & (a < F16_TINY)).sum()),
                         "subnormal": int(((a >= F16_TINY) & (a < F16_NORMAL)).sum())})
    return rows


def summary(rows: list[dict]) -> dict:
    total = sum(r["values"] for r in rows)
    return {"tensors": len(rows), "values": total,
            "max_abs": max((r["max_abs"] for r in rows), default=0.0),
            "non_finite": sum(r["non_finite"] for r in rows),
            "overflow": sum(r["overflow"] for r in rows),
            "to_zero": sum(r["to_zero"] for r in rows),
            "subnormal": sum(r["subnormal"] for r in rows),
            "worst": sorted((r for r in rows if r["overflow"] or r["to_zero"]),
                            key=lambda r: (-r["overflow"], -r["to_zero"]))[:10]}


def main(argv: list[str] | None = None) -> int:
    paths = [Path(p) for p in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    rows = [r for p in paths for r in scan(p)]
    s = summary(rows)
    print(json.dumps(s, indent=2))
    return 1 if s["overflow"] or s["non_finite"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
