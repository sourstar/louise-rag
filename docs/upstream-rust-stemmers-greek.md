# Upstream defect: rust-stemmers panics on Greek input

**Status in this project:** worked around. `config.STEMMER_EXCLUDE` keeps
Greek in its own index but drops the Snowball stemmer, because a panic
costs the *entire document's* indexing while missing stemming only costs
inflection matching. Revert the moment a fixed stemmer ships.

**Where it bit us:** one Project Gutenberg book (`gutenberg:31443`,
`lang=el`) out of 45,615 failed the fts stage with the unhelpful
`An error occurred in a thread: 'Any { .. }'`, which is how a Rust panic
crossing the FFI boundary reaches Python. 213 of 215 Greek books indexed
fine, because the defect fires only on one branch of the algorithm.

---

## Report (ready to post to https://github.com/CurrySoftware/rust-stemmers)

**Title:** Greek stemmer panics with "end byte index out of bounds" on
`παγαινετε`

**Version:** rust-stemmers 1.2.0 (latest release, 2019-11-17)

### Description

`Algorithm::Greek` panics on ordinary Greek input instead of returning a
stem. The panic happens inside the generated Snowball runtime:

```
thread '<unnamed>' panicked at src/snowball/snowball_env.rs:249:21:
end byte index 18 is out of bounds of `παγαινετ`
```

### Reproduction

```rust
use rust_stemmers::{Algorithm, Stemmer};

fn main() {
    let s = Stemmer::create(Algorithm::Greek);
    println!("{}", s.stem("πηγαινετε")); // ok -> πηγαινετ
    println!("{}", s.stem("παγαινετε")); // PANIC
}
```

The two inputs differ only in their second character (`η` vs `α`); both
are 9 characters / 18 UTF-8 bytes, both end in `-ετε`. Accents are not
required to trigger it — the corpus form `παγαίνετε` panics identically,
since the algorithm strips accents first.

### Analysis

The reported buffer, `παγαινετ`, is 8 characters / **16 bytes** — the
value *after* suffix removal — while the index being applied, **18**, is
the byte length of the *original* input. So a byte offset computed
against the pre-removal string is reused after the buffer has shrunk.
`παγαιν` appears to fall in one of the Greek algorithm's explicit
stem-exception branches, which is why the neighbouring real word
`πηγαινετε` is unaffected and why the defect is rare in practice.

### Impact

Consumers embedding this crate cannot catch a panic per token when the
call happens inside a worker thread — for us, one panicking word made an
entire document unindexable. A returned value (even the unmodified input)
would be strictly better than a panic for a stemmer.

### Environment

- rust-stemmers 1.2.0, as vendored by tantivy 0.26.0 (via the `tantivy`
  Python wheel, `tantivy.cpython-312-x86_64-linux-gnu.so`)
- Linux x86_64, Python 3.12
- Also reproducible through tantivy's own analyzer API with nothing but
  `Tokenizer::simple` + lowercase + `Filter::stemmer("greek")`

---

## Notes for whoever picks this up here

- The crate has published six versions, the last in **November 2019**;
  treat "wait for upstream" as "never".
- [`tantivy-stemmers`](https://lib.rs/crates/tantivy-stemmers) (0.4.0,
  June 2024) exists because of exactly this dormancy, is regenerated from
  the official Snowball repository, and includes Greek. Adopting it means
  compiling tantivy (and the Python bindings) from source, which trades a
  ~1 minute image build for ~10–20 and reopens on every version bump —
  the reason we took the one-line workaround instead.
- Our regression test (`tests/test_fts_greek_stemmer.py`) asserts the
  panic still happens, so if a future wheel fixes it the test fails and
  tells us the workaround can be removed.
