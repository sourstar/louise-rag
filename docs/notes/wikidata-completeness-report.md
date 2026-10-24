# Wikidata: what our collection reads, what it fixed, what it still leaves out (2026-10-24)

*Operator, 2026-10-24: "Wikidata fixes: fix our defects (i.e. uncertain
quantities) and wikidata Deutschland (write down in the report for them).
Historical facts: optimal solution and effort? Make a comprehensive report."
This note is that report. It gathers what the day decided and measured;
the details stay where they were written —
[wikidata-build-plan.md](wikidata-build-plan.md) (the design, the
measurements), [upstream-reports/wikidata.md](upstream-reports/wikidata.md)
(the report to the Wikidata team), the measurement outputs in
[wikidata-coverage-2026-10-24/](wikidata-coverage-2026-10-24/).*

**The record keeps everything.** A wikidata record holds the whole entity
as the dump states it (`extra`, invariant 11). Everything below is about
what we READ from it: the text that is searched, embedded and shown, the
title, the names, and which entities get more.

## 1. In one table

| | status | where |
|---|---|---|
| our defects in the text | **fixed** in parser 2: `mul` names of referenced entities, uncertain quantities (±), no id where a name exists | §2 |
| Wikimedia Deutschland's defects we had copied | **fixed** in our copy; **written** for them (W1, W6, W7 and the textifier's §1–5) | §3 |
| historical facts (what the best-rank filter drops) | **designed**: a history section in parser 3, nothing re-embedded; effort below, its size counted by tonight's run | §4 |
| references to merged entities, Wikimedia's own pages, lexemes, places' languages | open, each with its proposal | §5 |
| full text in it, de, fr, es for monuments, buildings, events | **approved**: the niche selection and a pilot first | §6 |
| the node | pack version 2 and wikidata-refs version 3 in place; Monday: wikidata created again on 6d02f8e, then its records | §7 |

## 2. Our defects — fixed in parser 2

Parser 2 is the parser Monday's records are built with: nothing has been
published under parser 1, so every fix below costs no recomputation (the
collection is created again from the same recipe, its calibration reused).

| defect | effect | fix |
|---|---|---|
| a referenced entity's label skipped `mul` (our copy dropped upstream's step) | 3.2 % of English texts wrote an id where a name exists ("PlayStation 2", every taxon named by its Latin `mul` label) | the chain upstream uses: the language, `mul`, the fallback |
| an id where a name exists (title, own name, referenced names, `ask lookup`) | 20.8 % of the records were titled by their id | names.py: the original name in Latin script, a Latin label (own wikis, then fr, es, it, de, then Wikidata's order), the original name, any label; the original language from P407/P364/P103 through each language's code P424 (2,215 codes) |
| a quantity's uncertainty dropped (our formatter) | "52±1.5 metre" read "52 metre" | `±` the larger distance to a bound, in exact decimals, as Wikibase shows it |
| Italian had no connectives | an Italian text would say "also known as" | Italian added (and offered upstream, W1) |

Found by reading our own code against upstream's and against the dump;
each with its test (tests/test_plugins_wikidata_textify.py,
tests/test_plugins_wikidata_names.py).

## 3. Wikimedia Deutschland's defects — fixed here, written for them

| # | what | in our copy | for them |
|---|---|---|---|
| T1 | aliases in random order (a Python set) — the text, and its vector, change from run to run | the dump's order | [wmde-wikidata-textifier.md](upstream-reports/wmde-wikidata-textifier.md) §1 |
| T2 | a failed date or coordinate format drops the value silently ("has date of birth") | our formatter has no service to fail | §2 |
| T3 | no Italian connectives | added | §3 (W1) |
| T4 | `novalue` written as "has <property>" — the opposite ("has architect" for "no architect"); `somevalue` loses its qualifiers (an unknown date's earliest/latest bounds) | "architect: no value", "date of death: unknown value (earliest date: 1450)", Wikibase's own wording | §4 (W6) |
| T5 | a lexeme, form or sense value written as the Python repr of its datavalue | its id (`L7`) | §5 (W7) |
| — | the Wikidata embedding project: stale vectors kept as current (~14 %), text depending on a live page, dropped entity data, the card's counts | — | [wmde-wikidata-embedding-project.md](upstream-reports/wmde-wikidata-embedding-project.md) |
| W2–W5 | data: a description cut short (Q22844), six P279 cycles, the most-referenced entities with no label in it/de/fr/es, the occurrence tree holding clinical trials, theses, statutory instruments, stars and coins | — | [wikidata.md](upstream-reports/wikidata.md) |

The operator files them; nothing has been filed yet. Before filing, W5's
P279 chains are traced (tonight's run prints the shortest chain per class).

## 4. Historical facts — the optimal solution and its effort

**What is left out.** Wikidata ranks every statement: preferred, normal,
deprecated. The textifier (upstream's choice, the "truthy" view the Query
Service calls `wdt:`) keeps, for each property, its preferred statements if
it has any, else its normal ones; deprecated never. So where a property has
a preferred value, its normal values vanish from the text — and those are
mostly HISTORY:
- a monument's former countries ("Papal States", until 1870) and former
  administrative places;
- former official names, former operators and owners, earlier heritage
  statuses;
- a city's former mayors; a building's earlier uses;
- and time series: every census population but the latest, every yearly
  visitor count, every GDP.

Nothing of it is lost from the record; it is missing from what is read
and searched. A question like "monuments of the Papal States" or "what was
X called before" cannot find it in the text.

**The options.**

| option | what the text gains | cost | verdict |
|---|---|---|---|
| A. as now (best rank) | nothing | none | leaves history out |
| B. every non-deprecated statement, inline | everything, time series included | every English text with history changes: its chunks re-embedded; time series bloat cities' texts (dozens of populations) | no |
| C. history inline, time series left out | the history | still changes the existing English chunks, so their vectors are recomputed | second best |
| **D. a separate history section** | the history, as its own section of the text | only the new section is embedded; the English text and its vectors stay byte for byte (carried by chunk hash, 06 §6.3, as a language section is) | **recommended** |

**The rule for the history section** (a pure function of the entity, so
records-vs-origin reproduces it):
- the statements the best-rank filter drops: a property's normal-rank
  statements when it has a preferred one; deprecated never;
- not quantities (the time series: the preferred, current value is already
  in the text) and not external identifiers (withdrawn ids);
- each with its qualifiers, so its dates come with it ("country: Papal
  States (start time: 1278, end time: 1870)");
- at most ten per property, the most recent first (by end time, then start
  time, then point in time; else the dump's order) — a city's thirty former
  mayors do not swamp its other history;
- headed in the text as its language's "history" part, so the word index
  files it with the English text (a heading `en` with a second part — a
  one-line change in `fts.by_section_language`).

**Where.** Parser 3, the daughter collection that also brings the niche
language sections (§6): one daughter, one re-issue of the changed records,
after Monday's English collection has its first snapshot. Parser 2 is not
touched: Monday goes ahead as planned.

**Effort.**
- Code: the rule in textify.py (the statements the filter drops, already
  computed there), the section in source.py, the heading in fts.py, tests —
  about a day, inside parser 3's work.
- Pack: nothing.
- Compute: the history sections' chunks only. Their number is tonight's
  count (`statements.dropped_by_rank`, over every entity and over the
  selection); the cost follows from it and the tokens a statement takes.
  OWED: filled in from the run (wikidata-coverage-2026-10-24/,
  classes-niche).

**The standard alternative, written down.** The full statement graph —
every rank, with qualifiers — is what the Query Service exposes as
`p:`/`ps:`/`pq:`. A history section is our reading of it for a text; it
adds no source and no service.

## 5. The rest of what the text leaves out

| what | proposal | effort | decision |
|---|---|---|---|
| references to entities merged into another (redirects) or deleted: written as ids — the JSON entity dump carries no redirects | name a merged entity by its target, read from Wikimedia's SQL table dumps (the `redirect` table, with `page` to map page ids to QIDs — a separate run, dated apart from the entity dump) | two acquisition files more (a new origin spec, so with parser 3; `page` is several GB), a `redirect` table in the pack | only if tonight's count (`to_absent_entities`) shows it matters |
| Wikimedia's own pages kept as records: categories, templates, disambiguation pages, lists | prune them as the scholarly articles are (whole entities, invariant 11 allows it), by the closure of "Wikimedia internal item" (Q17442446) | one line in the pruning rule; fewer records and fewer chunks to embed | counted tonight (`counted`); before Monday's records build only if decided tonight, else with parser 3 |
| Wikidata's lexemes: words, their senses and glosses in many languages (the separate lexeme dump, not taken by D1) | a small collection of their own — the natural home of "ancient and uncommon words" | a plugin variant over the lexeme dump (hundreds of MB, not 100 GB) | to decide |
| a place's original language: its country's official language (P37) | a third source for the original name in names.py | a country→language table in the pack | written down, not built |
| sitelink titles (a Wikipedia article's title) | — | — | left: mostly the label |
| statements' references (sources) | — | — | left: provenance, kept in the record |

## 6. Full text in more languages — the niche selection and the pilot (approved)

- **What:** a full-text section in Italian, German, French and Spanish,
  rendered from Wikidata's own labels, for monuments, buildings,
  archaeological sites, events, anything with a heritage designation, and
  the architectural vocabulary (barbican/barbacane, merlon; machicolation
  through architectural element).
- **Only where Wikidata says more in that language than in English:** a
  label in it and no English or `mul` one, a description in it and no
  English one, or a Wikipedia article in it and no English one. For the
  famous, the English text, the cross-lingual encoder and the all-language
  name search already work; the gain is in the local ones.
- **Measured:** 10.4 M entities in the first selection; Wikidata names
  them in Italian for 11.6 %, German 25.3 %, French 21.2 %, Spanish 12.7 %;
  a language for all would cost 1.61 G tokens (3.7 months of wikidata's
  budget). The niche counts and their cost, and the event roots cleaned of
  the occurrence tree's non-events, come with tonight's run.
- **The pilot, before the full embedding:** the Italian sections of a
  sample of the niche set embedded, Italian questions asked with and
  without them; the full run only if they win.
- **When:** parser 3, with the history section (§4), after Monday.

## 7. The node, and the order of work

1. Done today on the node: the pack upgraded in place to version 2 in
   37 min 38 s (2,215 language codes, 17,954,384 names; a rebuild from the
   dump is hours); wikidata-refs version 3 synced (121,724,461 rows; `ask
   lookup` now names Q3235171 "Les Roses de la vie"); a pack of newer code
   is never started over (München's safety check, now code).
2. Tonight: the classes run with the niche counts, the event roots' P279
   chains, the history, redirect and Wikimedia-page counts.
3. Monday: wikidata created again on 6d02f8e (parser 2), its text indexed,
   its records built, its first snapshot, `hold check`.
4. Then parser 3: the niche language sections, the history section, and
   whichever of §5 is decided — one daughter collection, the pilot first.
5. The operator files the upstream reports.
