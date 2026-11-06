//! `_cdc_boundaries` of app/chunking.py, compiled (docs/notes/python-hot-spots.md, "D").
//!
//! The chunker is FastCDC's normalized chunking over a Gear rolling hash
//! (Xia et al., USENIX ATC 2016): the gear table and both masks are the
//! Python module's own, handed in at construction, so there is ONE table and
//! it is derived where it always was (blake2b of each byte, chunking.py).
//! The loop below is a transliteration of the Python loop, statement for
//! statement; a boundary that moved would change wikipedia's space and
//! re-embed it (CLAUDE.md invariant 3), so nothing here is "equivalent" —
//! it is the same arithmetic, proven so:
//!
//! THE HASH. Python: `h = ((h << 1) + G[c]) & (2**64 - 1)`, on unbounded
//! integers, from `h = 0`. Here: `h = (h << 1).wrapping_add(G[c])` on u64.
//! By induction every Python `h` is below 2**64 (it is masked), so it is a
//! u64 equal to ours. Then Python's value is `(2h + G[c]) mod 2**64`, and
//! ours is `((2h mod 2**64) + G[c]) mod 2**64` — `<<` on u64 drops the bit
//! shifted out, `wrapping_add` drops the carry — which is the same residue,
//! since reduction mod 2**64 commutes with addition. Each `G[c]` is an
//! 8-byte digest read big-endian, below 2**64, so the table fits u64 exactly
//! (the constructor refuses one that does not).
//!
//! THE TEST. `h & mask` with `mask` 2**14-1 before the target and 2**10-1
//! from it: the same bits of the same number.
//!
//! THE CHARACTERS. Python indexes a `str` by CODE POINT and takes
//! `ord(ch) & 0xFF`. Here the text arrives as UTF-8 and is walked by
//! `char` — one Unicode scalar value, i.e. one code point — and each
//! becomes `(ch as u32) & 0xFF`; offsets count those, never bytes nor
//! UTF-16 units. (CPython keeps a non-ASCII `str`'s UTF-8 form until the
//! `str` is freed; an ASCII one is read in place.) A `str` holding a lone
//! surrogate has no UTF-8 form: it is not answered here (None), and the
//! caller runs the Python loop.
//!
//! THE OFFSETS. Every size and offset is a u64; a Python int past it is
//! not answered here (None). `max = 0` with text past `min` is the one
//! case the Python loop never leaves (it yields `start` for ever): None.
//!
//! Alternative written down (CLAUDE.md, "do not reinvent the wheel"): the
//! `fastcdc` crate implements the same algorithm family over BYTES with its
//! own table and masks, so its boundaries are not ours; and reading the
//! `str`'s PEP 393 buffer (UCS-1/2/4) directly would skip the UTF-8 form and
//! answer surrogates too, but leaves the stable ABI (one wheel per CPython).

use pyo3::prelude::*;
use pyo3::types::PyString;
use std::borrow::Cow;

/// The gear table and the two masks, as app/chunking.py defines them.
#[pyclass(frozen, module = "louise_cdc")]
struct Gear {
    table: [u64; 256],
    strict: u64,
    loose: u64,
}

impl Gear {
    /// `_cdc_boundaries` over the code points' low bytes: the end offsets,
    /// or None where the Python loop does not end.
    fn ends(&self, low: &[u8], min: u64, target: u64, max: u64) -> Option<Vec<u64>> {
        let length = low.len() as u64;
        let mut out: Vec<u64> = Vec::new();
        let mut start: u64 = 0;
        while start < length {
            let remaining = length - start;
            if remaining <= min {
                out.push(length);
                return Some(out);
            }
            let limit = remaining.min(max);
            let tgt = remaining.min(target);
            if limit == 0 {
                return None; // max == 0: the Python loop yields `start` for ever
            }
            // `for offset in range(min, limit)`, the mask chosen by
            // `offset < target`: the strict span, then the loose one. Both
            // are empty when `limit <= min`; `min < remaining` here, so
            // every index is inside the text.
            let window = &low[start as usize..(start + limit) as usize];
            let mut h: u64 = 0;
            let mut cut: Option<u64> = None;
            let strict_end = tgt.min(limit);
            let mut offset = min;
            while offset < strict_end {
                h = (h << 1).wrapping_add(self.table[window[offset as usize] as usize]);
                if h & self.strict == 0 {
                    cut = Some(offset + 1);
                    break;
                }
                offset += 1;
            }
            if cut.is_none() {
                offset = offset.max(tgt);
                while offset < limit {
                    h = (h << 1).wrapping_add(self.table[window[offset as usize] as usize]);
                    if h & self.loose == 0 {
                        cut = Some(offset + 1);
                        break;
                    }
                    offset += 1;
                }
            }
            let end = start + cut.unwrap_or(limit);
            out.push(end);
            start = end;
        }
        Some(out)
    }
}

/// Each code point's low byte, `ord(ch) & 0xFF`, in code-point order.
fn low_bytes(text: &str) -> Cow<'_, [u8]> {
    if text.is_ascii() {
        Cow::Borrowed(text.as_bytes())
    } else {
        Cow::Owned(text.chars().map(|ch| (ch as u32 & 0xFF) as u8).collect())
    }
}

#[pymethods]
impl Gear {
    #[new]
    fn new(table: Vec<u64>, strict: u64, loose: u64) -> PyResult<Self> {
        let table: [u64; 256] = table.try_into().map_err(|t: Vec<u64>| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "the gear table has one value per byte value, 256; got {}",
                t.len()
            ))
        })?;
        Ok(Gear {
            table,
            strict,
            loose,
        })
    }

    /// The table as it was handed in, for the caller to check.
    #[getter]
    fn table(&self) -> Vec<u64> {
        self.table.to_vec()
    }

    /// (strict, loose), as handed in.
    #[getter]
    fn masks(&self) -> (u64, u64) {
        (self.strict, self.loose)
    }

    /// `list(_cdc_boundaries(text, min_chars, target_chars, max_chars))`,
    /// or None when it cannot be answered here exactly: `text` not an exact
    /// `str`, a `str` with no UTF-8 form (a lone surrogate), a size that is
    /// not an integer in 0..2**64, or the Python loop's endless case.
    fn boundaries(
        &self,
        text: &Bound<'_, PyAny>,
        min_chars: &Bound<'_, PyAny>,
        target_chars: &Bound<'_, PyAny>,
        max_chars: &Bound<'_, PyAny>,
    ) -> Option<Vec<u64>> {
        if !text.is_exact_instance_of::<PyString>() {
            return None;
        }
        let (Ok(min), Ok(target), Ok(max)) = (
            min_chars.extract::<u64>(),
            target_chars.extract::<u64>(),
            max_chars.extract::<u64>(),
        ) else {
            return None;
        };
        // THE CONVERSION ERROR, caught: a lone surrogate (UnicodeEncodeError)
        // is dropped here and answered by the Python loop.
        let text = text.cast::<PyString>().ok()?.to_str().ok()?;
        self.ends(&low_bytes(text), min, target, max)
    }
}

#[pymodule]
fn louise_cdc(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Gear>()?;
    m.add("VERSION", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
