---
name: louise
description: Search a Louise library (books, articles, laws, encyclopedias and records, in many languages) through its MCP tools, and answer from what it holds, citing each source. Use when the reader asks about anything such a library may hold, or asks to search Louise.
---

# Louise

Louise is a library run by one node: collections of books, articles, laws,
encyclopedias and records, in many languages. Its MCP server
(`{{base}}/api/ask/mcp`) gives three tools: `louise_search`,
`louise_lookup` and `louise_traverse`. Answer only from what they return.

## Search: the reader's words first, then what you know

1. Search the reader's question as they asked it, with translation on:
   `louise_search {"query": "<the question, as asked>", "translate": true}`.
   `translate` also searches the query's translations into the languages
   the collections hold, as Louise's own Ask page does.
2. Read the results. When what the question asks for is not among them
   (the work it describes, the person, the law), search again with what
   you know about it:
   - for a work: its title and author as published, in the work's own
     language, then its English title (a question about a knight who
     tilts at windmills: `"Don Quijote de la Mancha Cervantes"`, then
     `"Don Quixote Cervantes"`);
   - otherwise: the names and key terms, without the question's filler.

   One idea per search, at most three more searches.
3. Keep to one collection with `collections` (`["gutenberg"]`) when the
   question is about one kind of source. A result's `uid` starts with
   its collection's plugin (`gutenberg:…`, `pubmed:…`).
4. A canonical id (`<namespace>:<id>`, e.g. `hgnc:1101`) is looked up
   with `louise_lookup {"id": "hgnc:1101"}`. A record's relations, such as
   editions, amendments and citations, are followed from its uid with
   `louise_traverse {"id": "<uid>"}`.

Leave the other parameters alone; their defaults are right. Set one only
when the reader asks for it: `as_of` for "as it stood on 2020-01-01",
`filters` for a category or a date range they name.

## Answer: only what the results say

- Answer only from the results. Never add a name, a date or a number
  they do not give. What you know goes into the searches, never into the
  answer.
- Cite each claim with the record's `uid` in square brackets, as the
  result gives it, e.g. `[pubmed:37000021]`. Cite a reference fact by its
  id, e.g. `[hgnc:1101]`. Never cite a record no tool returned.
- Say plainly when nothing answers, when a collection was withheld
  (`withheld`) and when a result is unverified (`trust_level`).
- Write in the reader's language, whatever language the results are in.
