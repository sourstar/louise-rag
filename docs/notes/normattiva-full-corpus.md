# Normattiva: the acts outside the 23 pre-packaged collections (2026-10-13)

Status: MEASURED, not built. The operator keeps `normattiva` as it is —
the 23 pre-packaged collections (review §10.1) — and asked how the full
body of Italian state law can be obtained for a new collection or a
daughter. This note is the answer: what the origin offers, measured on the
node against the live collection, and three designs with their costs. New
acquisition machinery is asked before built (CLAUDE.md); nothing here is.

## 1. The size of the gap

One advanced search with no filter returns the archive's facets: **205,100
acts** in Normattiva, by year of emanation. Against `validity_works`
(distinct acts held, any authority):

| Period | in the archive | held | missing | held |
|---|---|---|---|---|
| 1861–1947 | 130,487 | 127,213 | 3,274 | 97 % |
| 1948–1969 | 39,207 | 34,245 | 4,962 | 87 % |
| 1970–1989 | 22,496 | 19,299 | 3,197 | 85 % |
| 1990–2009 | 9,160 | 7,310 | 1,850 | 79 % |
| 2010–2026 | 3,750 | 2,950 | 800 | 78 % |
| **1948–2026** | **74,613** | **63,804** | **10,809** | **85 %** |

By type over the whole archive: regi decreti 91,346; DPR 47,761; leggi
32,699; regi decreti-legge 10,071; decreti luogotenenziali 7,554;
decreti-legge 3,867; decreti legislativi 2,928; decreti 2,539; the rest
under 1,500 each. The gap is ordinary laws above all: of 1990's 223 laws
the collection holds 98 (L. 185/1990, the pilot's absent gold act, is one
of the 125). The per-type shares quoted in review §10.1 (52–71 %) counted
`stato` keys against the year's shared numbering; the table above is the
archive's own count and supersedes them.

## 2. What the open-data API offers (measured 2026-10-13, manual rev. 09/01/2025)

Base `https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1`,
no auth. The manual is `dati.normattiva.it/assets/come_fare_per/API_Normattiva_OpenData.pdf`.

- **`POST ricerca/avanzata`** — the enumeration. Body as the manual gives
  it (`denominazioneAtto`, `dataInizioEmanazione`/`dataFineEmanazione`,
  `vigenza`, `classeProvvedimento`, `paginazione`); the response lists
  every act with `codiceRedazionale`, `dataGU`, number, date, type, and a
  `facetMap` by year, type and issuer. Page size 500 answered in 0.2 s;
  one year of laws is one page. The 500s of the first probes were my
  bodies, not the service; a form-encoded body is refused by the
  Poligrafico's protection (409).
- **`POST ricerca-asincrona/nuova-ricerca` → `PUT conferma-ricerca` →
  `GET check-status/<token>` → 303 with `x-ipzs-location` → the zip.**
  The export of a search as a zip of AKN (or XML, JSON, URI, HTML); at
  most 7,000 acts a job. Measured on L. 185/1990 alone: 202 with the
  token, confirmation without an e-mail, status 1 → 2 → 3 in about 50 s,
  a 28,831-byte zip with ONE member, `LEGGE_19900709_185/1990-07-14_090G0222_ORIGINALE_V0.xml`
  — the member naming of the pre-packaged zips. **One version per job**:
  the original by default, or the version in force at `dataVigenza`. There
  is no multivigente export outside the pre-packaged collections.
- **`POST ricerca/aggiornati`** — the acts updated between two dates
  (`dataInizioAggiornamento`/`dataFineAggiornamento`): the change signal
  the delta of normattiva-multivigenza.md §5 never had.
- **`POST atto/dettaglio-atto-urn`** — one article of one act as HTML by
  URN, `!vig=yyyy-mm-dd` or `@originale` selecting the version. A reading
  route for the reference source, not a corpus form.
- `tipologiche/denominazione-atto` — the 30 type codes (`PLE` legge, `PLL`
  decreto legislativo, `PDL` decreto-legge, `PPR` DPR, `COS` the
  Constitution, …), the `codice_tipo_provvedimento` of a job's filter.

Not a route: the portal's own per-act AKN export (`do/atto/caricaAKN`)
answers a browser session and refuses an automated client; it is not an
origin a verifier could reproduce, and it was not pursued.

One thing the archive makes certain: it serves one AKN per Expression
whatever the route — a version exported from the portal in a browser
session was byte-identical to the pre-packaged zip's member (203,586
bytes, the same `FRBRthis`). An act fetched through the API is the same
kind of unit as a member of the 23 zips, so the plugin, the parser and
`records-vs-origin` apply unchanged.

## 3. Three designs for the complement

**A. Daughter, original + current.** For every (type, year) with acts
outside the collections, two async jobs: the ORIGINALE export and the
VIGENTE export (`dataVigenza` = the pass's day). About 170 years × 2
forms, each job well under 7,000 acts, roughly 5 h of the service's time
and a few GB; the jobs re-export acts the collections already hold, which
the plugin keeps once (one copy per Expression, the M zip's by rank). Each
act gains its first and its last text; the VIGENTE AKN's `<lifecycle>`
lists every modification date, so the original's validity closes at the
first event and the current version opens at the last — and the versions
between are KNOWN to be missing, declared on the record (`in_force`
rules, review §3.2), never guessed. Answers "the law today" and "as
enacted" for every act; the temporal arm keeps to the 23 collections.

**B. Daughter, full history.** A's two jobs, then one job per (act,
intermediate version date) read from the lifecycle: filter
`numeroProvvedimento` + `annoProvvedimento` + type, `dataVigenza` the
version's date. The mother averages 1.67 Expressions per act, so the
10,809 acts need roughly 7,000 more jobs; at ~50 s each, serially, about
four days of a public service's time. Complete, honest, and heavy — the
etiquette question is the operator's to put to IPZS before the first job.

**C. Ask the origin.** The facets name exactly what is missing. A request
to the open-data team for a multivigente export of a custom search, or a
pre-packaged "Leggi" collection, costs nothing to make and would hand the
complement in the form the 23 collections already have.

Recommendation: C now, in writing; A as the daughter's first step whatever
C answers, because it closes "the law today" for every act with existing
machinery plus one spec mode; B only for the acts the pilot shows matter.

## 4. The machinery A needs (to ask before building)

- An acquisition spec mode beside `zip-directory`: **`api-export`** —
  enumerate by `ricerca/avanzata` facets, run the async jobs in order,
  store each zip under the origin as `akn/E/<form>/<year>.zip`, verify by
  the zip's directory as today; `ricerca/aggiornati` since the last pass
  decides which (type, year) partitions are re-exported. Two connections,
  the token bucket (invariant 5), jobs one at a time.
- The daughter's Codex (02 §2.7): `continues` normattiva at its last
  snapshot; `space`, `layers`, `cleaning_version` byte for byte; the spec
  adds the export origin and records the form choice with its reason
  (invariant 11): "the API's export, the only automated route; one version
  per job". Records of the 23 collections stay byte-identical (same
  members, same `origin_hash`) and are inherited with their vectors; the
  new acts are the daughter's.
- Slicing continues by type and year; the Constitution (`COS`, one act)
  arrives with 1947's export — the open point of
  normattiva-multivigenza.md §3 closes with it.
- The validity rule for a record whose intermediate versions are absent:
  the original's end = the first lifecycle event, the current's start = the
  last; a query dated between them finds no version of that act and says
  so (05 §5.1's withheld leg, never a wrong text).

## 5. Measurements to repeat before the first job

The job time at 1,000 and 7,000 acts (one act took 50 s; the service may
scale per act); the zip sizes for a whole year of DPR; whether a VIGENTE
export of an act with no amendments names the original's date; the
`aggiornati` feed's granularity (does a consolidation count as an update).
