# Louise in Open WebUI

Three ways, from the least to the most like the Ask tab. `/connect` on
the node shows the first two filled in for the person reading it.

1. **As models.** Admin Settings → Connections → OpenAI API → `+`. URL
   `<louise>/v1`, API key: a Louise key. `louise` searches everything,
   and `louise/<collection>` searches one collection.
2. **As a tool (MCP).** Admin Settings → External Tools → `+`. Type MCP
   (Streamable HTTP), URL `<louise>/api/ask/mcp`, Bearer with a Louise
   key. The chat's own model decides when to search.
3. **As a pipe** (`louise_pipe.py`, this folder). This is the closest to
   the Ask tab.
   - Admin Panel → Functions → `+`. Paste the file, save, and enable it.
   - Its Valves (gear icon):
     - `LOUISE_URL` is where Open WebUI reaches Louise.
     - `PUBLIC_URL` is where a browser does, for the source links.
     - `LOUISE_TOKEN` is the key from `louise user add openwebui`.
   - The model menu then shows "Louise: all collections" and one model
     per collection.
   - In a chat, Controls → Valves lets each person set:
     - `COLLECTIONS`: several, comma-separated;
     - `ANNOTATIONS`: `all`, `none` or names;
     - `ANSWER_IN`, `RESULTS` and `ALL_LANGUAGES`;
     - their own `TOKEN`.
   - Each answer's sources are numbered as its `[n]`. They carry the
     passage, the work's other editions and the claims beside it, and
     each links to Louise's reader. The reader needs the person logged in
     to Louise in that browser.

One search per question: the pipe asks `/v1/chat/completions` with
`sources: true`, and Louise returns the hits with the answer.

Set a **Task Model** in Open WebUI (Admin Settings → Interface).
Otherwise the pipe answers Open WebUI's title, tag and follow-up
requests itself, cheaply, without searching: the title is the
question's first 60 characters, and there are no tags or follow-ups.

Not carried over from the Ask tab:
- translating the hits;
- the pictures inline;
- facet filters.
