# PubMed: a record whose only abstract is an OtherAbstract embeds its title alone

*Found 2026-10-19, 01:29, by pubmed-v8's vectors-sample during its switch
([char-cut-truncation.md](char-cut-truncation.md) §5). A decision for the
operator at the end.*

## What was found

One sampled shard (year=1995-1999) missed its quorum: 9 of its 16 rows under
the tolerance (0.999914), all NCBI's MedCPT vectors (`genesis:medcpt-pubmed`),
all title-only rows — "Editorial.", "[Young people first].", "Japan's TFR
plunges to record low of 1.43." — PMIDs 12,287,128–12,321,495.

Their records hold an abstract the origin states: an `OtherAbstract` of type
"PIP" (POPLINE's), kept since parser 8 in `extra.other_abstracts`. But the
record's `sections` are `[Title]` (+ `Abstract` where `<Abstract>` exists),
and the pair layer cuts and embeds the sections: the title alone. NCBI
embedded title AND that abstract. So NCBI's vector is not this space's
function for those records.

## What was done (02 §2.4's remedy, RULE 1)

`tools/pubmed_other_abstract_rows.py` read the snapshot's parts once:
39,994,988 records, **63,921 cut to a title alone beside an other abstract**,
54,548 of them NCBI's rows (the other 9,373 were embedded here, from the
record's own input, and are right). The 54,548, and one more title-only row
of unknown cause (PMID 5158766, no other abstract), were dropped and their
documents marked to embed again (`louise dev drop-rows`, 54,549 rows). They
are re-embedded from their records: the title alone, the space's function.

## The decision (operator)

The space is now right, and those 63,921 documents are found by their title
only, although the origin gives each an abstract. Embedding the other
abstract where there is no `<Abstract>` would give them what NCBI's vectors
had: parser 9 (`sections` gains an "Abstract" from the first other abstract
when `<Abstract>` is absent — or from all of them), so `content_hash` moves
for those records alone, and a new Codex carried by A1c (`--rebuilt`,
`--reconcile`): ~64 k documents embedded again, every other vector kept.
Which other abstracts count (PIP only, every type, the English one first) is
the operator's to say.
