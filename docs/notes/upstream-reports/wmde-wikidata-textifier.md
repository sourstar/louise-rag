# wmde/WikidataTextifier — two issues that make an entity's text irreproducible

Tested at commits 0befd7c and fa32546, on rows of `philippesaade/wikidata`
(dump 2026-05-07), against the vectors of `philippesaade/Wikidata_Vectors_0.2`.

## 1. Alias order is random

**Root cause:** `Normalizer/JSONNormalizer.py`, in `normalize()`, builds the
aliases from a set:
`aliases = list({a.get("value") if isinstance(a, dict) else str(a) for a in aliases if a})`.
Python randomises string hashing per process, so the order (and the text,
and the vector) changes from one run, or one worker, to the next.

**Example:** Q9137738 renders as "SMS A2, also known as A2, A-2." in one run
and "… A-2, A2." in another. The published vector matches only the second
(cosine 0.99996, against 0.9979 for the first). In a sample of 1,000 English
entities, 7 of 139 mismatching vectors are only this.

**Fix:** `aliases = list(dict.fromkeys(...))`. It keeps the dump's order,
without duplicates.

## 2. A failed date or coordinate format is silently dropped

**Root cause:** `JSONNormalizer._to_value_object` catches
`(ValueError, TypeError, KeyError, requests.RequestException)` from
`wikibase_time_to_text` (a call to the Wikibase formatter) and returns
`None`. The claim then renders as "has date of birth", and the item is
embedded without the value and without an error.

**Example:** with the formatter unreachable, Q9166048 becomes
"- has date of birth" instead of "- date of birth: 1973". On 1,000
entities, the exact-match rate to the published vectors falls from 87 % to
57 %.

**Suggested fix:** raise rather than return `None` for a network error, so
the item fails and is retried instead of being embedded degraded.
