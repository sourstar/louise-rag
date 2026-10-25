# Collections to come — nice to have, not planned

*A register of collections worth having one day, with why, what they would
take, and their pros and cons. Nothing here is scheduled; an entry moves
to a plan of its own when the operator asks for it.*

## Wiktionary — words, their meanings and their history

*Operator, 2026-10-24: "Wiki dictionary: is it part of wikidata? … If no,
write it down as a nice to have future collection."* Wiktionary is a
Wikimedia project of its own, not part of Wikidata: one dictionary per
language edition (it.wiktionary, en.wiktionary, …), written as wikitext.

- **What it would give:** full definitions, etymologies, senses tagged
  archaic, obsolete or regional, translations, inflections — "barbacane"
  with its meanings, where it comes from, how it is used. The natural
  answer to "ancient and uncommon words".
- **The proven way in:** wiktextract (kaikki.org), the standard extractor
  that turns each Wiktionary dump into structured JSON per word; do not
  write a wikitext parser of our own.

| | pros | cons |
|---|---|---|
| Wiktionary via wiktextract | far richer than anything structured: full definitions, etymologies, the rare and archaic senses, many more Italian words | CC BY-SA: a recorded licence acknowledgement (yellow class, invariant 9) and share-alike on what is derived and shared; the origin form must be settled (the Wiktionary dump, re-extracted by a pinned wiktextract, so records reproduce — invariant 11), not kaikki's files taken as they are; each language edition is its own dump |

## Wikidata's lexemes — the dictionary inside Wikidata

**Not a collection to come: a part of `wikidata`** (operator, 2026-10-24:
"everything belonging a collection must be part of the collection; if
structurally independent it can be an optional download and/or
embedding"). The lexemes join the `wikidata` collection as a slice of
their own (`family=lexeme`), optional to hold and to embed; the plan is in
[wikidata-family-slices.md](wikidata-family-slices.md). What follows is the
background.

Lexemes are one of Wikidata's four kinds of entity (below). The `wikidata` collection takes items and properties only
(the "all" dump, decision D1); the lexemes come as a dump of their own,
in their own dated runs of the same tree (`wikidatawiki/entities/<date>/`,
`wikidata-<date>-lexemes.json.bz2`; München saw one in 20261014).

- **What it would give:** a word (lemma, language, part of speech), its
  forms ("barbacani" finds "barbacane"), its senses with short glosses, the
  concept each sense means (a link into the `wikidata` collection),
  translations, synonyms, etymology, pronunciation.

| | pros | cons |
|---|---|---|
| the lexemes as a slice of `wikidata` | CC0 (green, no acknowledgement); structured, nothing to parse; the `wikidata` plugin's dump reader, pack and textifier reused; links words to the things they name; small, so cheap to embed whole | glosses are one line and many lexemes have no sense at all; Italian coverage is to be counted (one CPU pass on the node); a plugin variant and a collection, a few days of work |

Order if wanted: count the Italian lexemes first; the lexemes before
Wiktionary (CC0, linked to what we hold); Wiktionary where they fall short.

## Wikidata's four kinds of entity

| kind | ids | what it is | in the `wikidata` collection |
|---|---|---|---|
| items | Q… | every thing, concept, person, place, work — 121.9 M with the properties in the 2026-10-12 dump, of which 46.0 M scholarly articles (pruned by D1 until 2026-10-24; under the operator's rule they become the optional `scholarly` slice) | yes |
| properties | P… | the statements' vocabulary (~12 k) | yes |
| lexemes | L… (forms L…-F…, senses L…-S…) | words, with their forms and senses | to join, as the `lexeme` slice (above) |
| entity schemas | E… | ShEx shapes describing how items should look; not data about the world | no |
