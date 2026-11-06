# Translate before embedding? A measurement on gutenberg

*Measured on the node 2026-10-08. No spec or product code changed; the script is
[`tools/measure/translate_before_embed.py`](../../tools/measure/translate_before_embed.py).*

**Answer: no.** For the languages the encoder does not list, translating with
MADLAD before embedding made retrieval **worse**. MRR fell on both query types,
and on cross-lingual queries 37 books got worse against 15 that got better. For
supported mid-resource languages (de, fi, nl, fr) it helped English
metadata-style queries a little (MRR 0.245 → 0.297), but it hurt same-language
retrieval in a mixed pool, because it erases the language the passage is
written in. A passage costs ~7.6 s of shared GPU time to translate.

## Setup

- **Collection.** gutenberg's SHARD on tank (`records/lang=*/NNNNN.jsonl.zst`),
  read with `app.plugins.drag.shard_files` and `app.pack.records`, mounted
  read-only. The collection file was mounted read-only as well.
- **Encoder.** gutenberg's own recipe through `embeddings.embed_chunks`
  (passages) and `embeddings.embed_query` (queries): qwen3-embedding-4b on the model pool,
  f16, dim 2560, no query instruction. Every answer named one run,
  `sha256:e31ca925b843…c516b5`.
- **Translator.** `translate.translate(…, "en")` against Louise's
  `TRANSLATE_URL`, read with the code's own resolution:
  `…:30437/upstream/madlad`, MADLAD-400 on the model pool. Source language is not given,
  as in Louise.
- **Container.** A throwaway `xlate-measure` container (`--rm`, `nice -n 10`)
  from `louise-rag:local` (sha256:af87ba0c…). That image was built at 15:16,
  before the embedding entry points existed (838c005, 17:45). So the committed
  `app/` at `ade60fe` was mounted read-only over `/srv/app`. The dispatch level
  was `corpus`. Nothing under `/data`, no manifest and no lance table was
  mounted. The SHARD and the config were read-only.
- **Rare set.** gutenberg languages missing from Qwen3's 119-language list.
  - *Source.* The Qwen3-Embedding-4B card says only "100+ languages … inherits
    Qwen3", so the list is the one in the Qwen3 blog,
    qwenlm.github.io/blog/qwen3.
  - *Languages.* la, eo, fy, enm, arp, gla, sco, sa, oji, nah, myn, mi, iu, br,
    bo, nap, rmq, ia.
  - *Listed, so left out.* tl, ceb, ilo ("Iloko"), oc, fur, gl, cy, ga, is,
    af, yi ("Eastern Yiddish"), no.
  - *Also left out.* el, Greek: listed, though some of gutenberg's el books are
    Ancient Greek. And und.
- **Controls.** en, fr, de, fi, nl.
- **Sample.**
  - *Books.* Up to 20 books per language, drawn with a seeded shuffle; books
    whose body is under 6,400 chars were skipped. la, eo and fy have 20 each,
    enm 3 and gla 2. The other twelve rare languages have **one book each**
    in gutenberg.
  - *Passages.* Each book gives one document passage, starting at 15–45 % of
    the body, and one query passage at 55–85 %, never overlapping. Each is
    1,200–2,000 chars, ~300–500 tokens by `chunking._est_tokens`. That is
    smaller than gutenberg's 1,000-token chunks.
- **Pools.** RAW holds the 178 original passages plus 500 English distractor
  passages from random English books. TRANSLATED holds the 178 translations
  plus the same 500 distractors, carried as the same vectors. Both pools hold
  678 passages.
- **Queries.**
  - *Q1, cross-lingual topic.* An English query: the title (MADLAD-translated
    for non-English books), then the LCSH subjects and the bookshelves. The
    same vector is used against both pools.
  - *Q2, same-book.* The book's other passage: the original for RAW, its
    translation for TRANSLATED.
  - *Target.* The book's document passage. Ranking is by cosine.
- **Within-language view.** Each query is also ranked against only its own
  language's sampled passages, with no distractors (languages with ≥ 5 books).
  In the mixed pool a raw passage can be found by its language alone (a Frisian
  query passage finds Frisian text; "Frisian poetry" in Q1 points at the
  Frisian passages). Translation removes that cue. This view asks only "which of
  these books is it?".

## Results

Recall@1 / @5 / @10 and MRR. "paired" counts the books where TRANSLATED ranked
the target better / worse / the same.

### Groups, mixed pool (678)

| group | n | Q1 RAW | Q1 TR | Q1 paired | Q2 RAW | Q2 TR | Q2 paired |
|---|---|---|---|---|---|---|---|
| rare | 78 | .32/.56/.67 · .434 | .26/.47/.54 · .368 | 15 / **37** / 26 | .62/.83/.86 · .696 | .46/.55/.72 · .525 | 8 / **33** / 37 |
| rare, passage not mostly English¹ | 71 | .31/.56/.66 · .428 | .24/.46/.52 · .356 | | .65/.87/.89 · .728 | .48/.56/.73 · .537 | |
| controls fr de fi nl | 80 | .17/.29/.38 · .245 | .23/.40/.45 · .297 | **41** / 22 / 17 | .42/.59/.68 · .513 | .29/.45/.55 · .379 | 7 / **47** / 26 |
| en (en→en pass) | 20 | .30/.50/.55 · .383 | .30/.40/.50 · .377 | 5 / 9 / 6 | .45/.60/.60 · .493 | .40/.55/.60 · .465 | 7 / 5 / 8 |

¹ Share of English function words under 0.15. This drops the 3 enm books, sco
and bo. bo's sampled passage is the English half of a bilingual book.

Sign tests:
- **Rare, Q1:** 15 better against 37 worse, p ≈ 0.004.
- **Controls, Q1:** 41 better against 22 worse, p ≈ 0.02.

### Per language, mixed pool (MRR; r@1 in brackets)

| lang | n | Q1 RAW | Q1 TR | Q2 RAW | Q2 TR |
|---|---|---|---|---|---|
| la | 20 | .518 (.40) | .483 (.30) | .772 (.75) | .732 (.70) |
| eo | 20 | .323 (.15) | .337 (.25) | .711 (.60) | .487 (.40) |
| fy | 20 | .271 (.15) | .165 (.05) | .489 (.35) | .219 (.15) |
| enm | 3 | .399 | .388 | .833 | .833 |
| gla | 2 | .508 | .505 | .667 | .502 |
| singletons² | 13 | ranks below | | | |
| en | 20 | .383 (.30) | .377 (.30) | .493 (.45) | .465 (.40) |
| fr | 20 | .279 (.20) | .287 (.20) | .373 (.30) | .267 (.15) |
| de | 20 | .311 (.25) | .363 (.30) | .594 (.55) | .536 (.45) |
| fi | 20 | .169 (.10) | .263 (.20) | .678 (.60) | .469 (.40) |
| nl | 20 | .223 (.15) | .276 (.20) | .406 (.25) | .242 (.15) |

² Singletons, target rank RAW → TRANSLATED for Q1 and Q2. One book each, so
these are anecdotes:

| outcome | lang | Q1 | Q2 |
|---|---|---|---|
| unchanged | arp | 1 → 1 | 1 → 1 |
| unchanged | sa | 1 → 1 | 1 → 1 |
| unchanged | nah | 1 → 1 | 1 → 1 |
| unchanged | myn | 1 → 1 | 1 → 1 |
| unchanged | nap | 1 → 1 | 1 → 1 |
| unchanged | oji | 3 → 3 | 1 → 1 |
| worse | mi | 1 → 13 | 1 → 16 |
| worse | br | 1 → 131 | 1 → 18 |
| worse | iu | 1 → 3 | 1 → 1 |
| worse | ia | 53 → 115 | 1 → 2 |
| better | rmq | 214 → 64 | 1 → 1 |
| better | bo | 71 → 46 | 159 → 67 |
| better | sco | 1 → 1 | 69 → 50 |

The unchanged ones are mostly cases where MADLAD copied the text through
untranslated.

### Within-language view (r@1 · MRR, 20 books each, no distractors)

| lang | Q1 RAW | Q1 TR | Q2 RAW | Q2 TR |
|---|---|---|---|---|
| la | .75 · .825 | .55 · .691 | .75 · .828 | .75 · .834 |
| eo | .50 · .629 | .60 · .691 | .60 · .729 | .55 · .710 |
| fy | .15 · .302 | .10 · .251 | .35 · .494 | .20 · .317 |
| en | .70 · .796 | .60 · .737 | .60 · .717 | .65 · .746 |
| fr | .40 · .516 | .45 · .531 | .35 · .501 | .45 · .589 |
| de | .55 · .706 | .50 · .649 | .60 · .722 | .75 · .812 |
| fi | .55 · .686 | .40 · .607 | .60 · .693 | .60 · .672 |
| nl | .60 · .715 | .60 · .724 | .45 · .617 | .50 · .663 |

Within one language, translation is roughly neutral for the controls: it helps
same-book retrieval slightly (fr, de, nl) and is mixed on Q1. So the large Q2
loss in the mixed pool is mostly the lost language cue, not worse vectors. For
the rare set it is not neutral: Latin Q1 drops 0.20 at r@1 and Frisian drops on
both queries. Esperanto is the one rare language where translation helps Q1.

## Cost

| | |
|---|---|
| translation, total | 2,694 s for 356 passages (178 books × 2) — **7.6 s / passage**, ~60 est. tokens/s in and out |
| per language | 5.5–7 s/passage for fr, de, fi, nl, en; la 9.4; fy 13.1; oji, myn, br, arp 12–15.5 |
| failures, stops | none failed; none hit the 15-min-per-language stop (worst: fy at 524 s) |
| embedding | 1,390 calls (500 distractors, 356 passages, 534 queries) in 403 s, ≈ 0.29 s each, EMBED_BATCH 8, concurrency 1 |

At 7.6 s per 400 tokens, one 1,000-token gutenberg chunk is ~19 s of MADLAD on
the shared card. The embedding of that chunk costs well under a second.

## What the translations look like (first ~200 chars)

| lang | source | MADLAD → en |
|---|---|---|
| arp | Haudnauthedaudauwau hethadee jaunaunauau jaje Vahadāde. | (copied verbatim) |
| nah | Auh yn achto otiquinteneuhque huehuetque yn iuh quitotihui… | Auh yn prome otiquinteneuhque huehuetque… (copied, one word changed) |
| myn | …Cha cu nu tzih chuvach cah, chuvach uleu… chupam r'al nu chab | …(copied; "chupam" → Spanish "dentro") |
| sa | pUtaatmaa paramaatmaacha muktaanaam paramaagatih… | (copied verbatim) |
| oji | Nimidana sa dasogwan iwipi keiabi o gi widjaiawan… | When he was asked to give his opinion on the subject, he said that he was not categorized as a Catholic. … When the blade is broken… (invented) |
| iu | naglingnernik. Kairĸojaumanîgik anerijaumanîgiglo… | I'm not sure what you're saying. Taimak illagêt… (Catty uses patting tool) Bed. (invented, then copied) |
| mi | I a Hinemoa ano e whakamahana ana i a ia i roto i te waiariki, ka pa te hiainu ki a Tutanekai… | When Hymenmethus was still warming himself in the water, he came to Tutankhamun… (names mangled, plot wrong) |
| br | Sarromp ama mar kirit levr an traou a zianveaz ha digoromp eul levr all… | …si retrivit libro de las cosas que han sido hechas y abrimo un otro libro… (**into Spanish**) |
| bo | Regard others, who (equally) follow the teaching, as their veriest enemies. | They are the ones who are the most likely to be the most successful. (English source garbled) |
| gla | Tha Aoidh no Uisdean Muillear ag radh anns an leabhar aige, "Scenes and Legends,"… | Aoidh or Uisdean Muillear says in his book, "Scenes and Legends," that his mistress came to his ionnsuidh with a poisoned dinner… (good) |
| la | _SO._—An non bonas honorare decet, mala vero contemnere? _CR._—Ita decet. | SO. An non bonas honorare decet, mala vero contemnere? _CR._—It's decent. (mostly left in Latin) |
| la (title) | Sertum Anglicum, seu, Plantae Rariores… | The first English-language book, Plants Rarely Found in the Gardens Near London, was published in 1786 and was published in 1787. (invented) |
| eo | "Ha, estas vi, _Leone_? Nun ĉu vi alportas novaĵon de ekstere?" | "Ha, are you Leona, bringing news from outside?" (good) |
| fy | FRÂNSKE. (_nei in pear tellen twivel_). Ate.... | FRÂNSKE. (_after a few counts of doubt_). Ate.... (good) |
| rmq | 7. Y quereláron simaches á os averes manuces sos sinaban andré o aver berdo… | 7. And they begged the bears, that they would help them… (invented) |
| ia | E depois il habeva le capite de Myrtle. Crederea tu… | And then he had the head of Myrtle. Would you believe… (good) |

All examples, two per rare language, are in the run's `examples.json` on the
node.

MADLAD reads what it was trained on: Esperanto, Frisian, Interlingua, Scottish
Gaelic. On the truly rare languages (Arapaho, Nahuatl, Mayan, Sanskrit in
ITRANS) it copies the text through, or it invents fluent English unrelated to
the source (Ojibwa, Inuktitut, Caló, Māori). It once switched target language
(Breton → Spanish). Invented text is the worst case for retrieval: a fluent
English vector in the wrong place. The raw passage at least stays an honest
"text in language X".

## Conclusion

1. **Rare languages: translation does not win, it loses.** The encoder
   already handles Latin, Esperanto and Frisian passably raw. Where it surely
   does not (the singletons), MADLAD does not know those languages either.
   Translating would add a ~19 s/chunk GPU cost to make retrieval worse.
2. **Supported controls: a small gain on English queries, at a price.**
   Translation modestly improves English metadata-style queries for de, fi and
   nl (fi Q1 MRR 0.17 → 0.26). It removes the language identity a raw vector
   carries, so in a mixed pool same-language retrieval drops (MRR 0.51 →
   0.38). Within one language it is neutral to slightly positive.
3. **Query-side translation already covers this.** The cross-lingual gain that
   exists is the one translation already gives at the query side (05 §5.2, an
   expand step: the query into the indexed languages), at a fraction of the
   cost. Nothing here argues for translating documents before embedding.

**Limits.**
- *Sample size.* Twelve rare languages have one book each, so their numbers
  are anecdotes. The rare-set verdict rests on la, eo and fy (60 books) and
  the sign test.
- *Query quality.* Q1 queries are coarse LCSH metadata. Many Frisian books
  share "Frisian poetry", which caps fy's Q1.
- *Sampling.* One passage per book, of 300–500 tokens rather than gutenberg's
  1,000-token chunks.
- *Distractors.* All 500 are English.
- *English-share heuristic.* It is a function-word count.

## Reproduce

On the node, `/root/xlate-measure/run.sh [sample|translate|embed|all]` starts
the container described above, with output in `/root/xlate-measure/out/`. Each
phase resumes from its checkpoint (`sample.json`, `translations.json`,
`vectors.npz`), and `results.json` holds every number above, per book included.
