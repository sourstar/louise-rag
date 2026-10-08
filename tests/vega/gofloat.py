"""A float32 as LocalAI v4.11.0 writes it in JSON: Go's encoding/json, the
shortest decimal that reads back to the float32 (the closest of those), 'f'
or 'e' by the ES6 cutoffs, `-0` for negative zero (tools/vega_f32_json.go).

Exact, with fractions: the fake LocalAI writes its vectors with it, and
tests/fixtures/vega/go_f32_json.txt — written by Go itself — holds it to Go.
"""
from __future__ import annotations

import struct
from fractions import Fraction

_F32_1EM6 = struct.unpack("<f", struct.pack("<f", 1e-6))[0]
_F32_1E21 = struct.unpack("<f", struct.pack("<f", 1e21))[0]


def bits_of(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def value(bits: int) -> Fraction:
    """The exact value of a float32's bits, magnitude only; exponent 255 with
    mantissa 0 reads as 2^128, the step above the largest float."""
    e, m = (bits >> 23) & 0xFF, bits & 0x7FFFFF
    if e == 0:
        return Fraction(m, 1 << 149)
    return Fraction((1 << 23) | m) * (Fraction(2) ** (e - 150))


def _floor_log10(v: Fraction) -> int:
    k = len(str(v.numerator)) - len(str(v.denominator))
    while Fraction(10) ** k > v:
        k -= 1
    while Fraction(10) ** (k + 1) <= v:
        k += 1
    return k


def _digits(bits: int) -> tuple[str, int]:
    """(digits, exponent): the shortest decimal int(digits) * 10**exponent
    that reads back to the float32, the closest to it when several do."""
    v = value(bits)
    lo = (v + value(bits - 1)) / 2 if bits & 0x7FFFFFFF else Fraction(0)
    hi = (v + value(bits + 1)) / 2
    mant = (bits & 0x7FFFFF) | ((1 << 23) if (bits >> 23) & 0xFF else 0)
    inclusive = mant % 2 == 0
    k = _floor_log10(v)
    for p in range(1, 10):
        unit = Fraction(10) ** (k - p + 1)
        q = v // unit
        best = None
        for c in {q, q + 1}:
            x = c * unit
            ok = lo < x < hi or (inclusive and x in (lo, hi))
            if ok and (best is None or abs(x - v) < abs(best * unit - v)
                       or (abs(x - v) == abs(best * unit - v) and c % 2 == 0)):
                best = c
        if best is not None:
            text = str(best)
            stripped = text.rstrip("0")
            return stripped, (k - p + 1) + (len(text) - len(stripped))
    raise AssertionError(f"no shortest form for {bits:08x}")


def go_json(x: float) -> str:
    """`x`, a float32 value, as encoding/json writes a float32."""
    bits = bits_of(x)
    sign = "-" if bits >> 31 else ""
    mag = bits & 0x7FFFFFFF
    if mag >= 0x7F800000:
        raise ValueError("json: unsupported value")          # NaN, ±Inf: LocalAI's B9
    if mag == 0:
        return sign + "0"
    d, e = _digits(mag)
    a = abs(struct.unpack("<f", struct.pack("<I", mag))[0])
    if a < _F32_1EM6 or a >= _F32_1E21:
        exp = e + len(d) - 1
        mantissa = d[0] + ("." + d[1:] if len(d) > 1 else "")
        return f"{sign}{mantissa}e{'-' if exp < 0 else '+'}{abs(exp):02d}".replace("e-0", "e-")
    if e >= 0:
        return sign + d + "0" * e
    n = len(d)
    if -e < n:
        return f"{sign}{d[:n + e]}.{d[n + e:]}"
    return f"{sign}0.{'0' * (-e - n)}{d}"
