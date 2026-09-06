# Upstream defect: rust-stemmers panics on Greek input

**Status in this project:** replaced, not worked around. Greek is listed in
`config.PYSTEM_LANGS`, so it is stemmed by **PyStemmer** (libstemmer, the
Snowball project's own C implementation) in `app/stemming.py`, and tantivy
is told not to stem it. Greek keeps full stemming; nothing is lost.

The first fix was weaker — drop stemming for Greek entirely — on the
grounds that a panic costs the whole document while missing stemming only
costs inflection matching. That trade is no longer necessary.

Why PyStemmer was chosen, checked BEFORE adopting it (the lesson from
inheriting rust-stemmers transitively and discovering its state only when
it crashed):

| | rust-stemmers 1.2.0 | PyStemmer 2.2.0.3 |
|---|---|---|
| last release | 2019-11-17 | 2024-10-10 |
| releases ever | 6 | actively maintained |
| languages | fewer | 29 |
| cp312 manylinux wheel | n/a (vendored in tantivy) | yes, 683 kB, no toolchain |
| Greek `παγαινετε` | **panics** | stems cleanly |

Verified on the real corpus before the switch: identical stems to
rust-stemmers on every word rust-stemmers manages (`ανθρωπ`, `βιβλι`,
`καλημερ`, `πηγαινετ` — same Snowball algorithm, different implementation),
and zero exceptions across all 6,243 distinct tokens of the book that used
to fail.

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
  June 2024) exists because of exactly this dormancy and is regenerated
  from the official Snowball repository. We did NOT need it: adopting it
  means compiling tantivy and its Python bindings from source, trading a
  ~1 minute image build for ~10–20 and reopening at every version bump,
  whereas PyStemmer is a prebuilt wheel that solves the same problem in
  our own process.
- Our regression test (`tests/test_fts_greek_stemmer.py`) asserts the
  panic still happens. If a future wheel fixes rust-stemmers, that test
  fails — which is the signal that Greek could go back to tantivy's
  filter, should anyone want to.
- **Other languages still stem inside tantivy**, on rust-stemmers. That is
  a cost decision, not an endorsement: moving a language to PyStemmer
  changes its index schema, so it must be re-indexed, and pubmed's ~20M
  documents would cost weeks of CPU to fix a bug they do not have. The
  mechanism is general — add the language to `PYSTEM_LANGS`, delete that
  group's index directory, reset `fts_done` for those documents.
