# Louise — documentation

**Louise — knowledge, kept.**

Louise is a RAG platform you run yourself, online or off. It comes with
Wikipedia, Project Gutenberg, PubMed and the genome databases, and any
other source can be added by implementing a plugin. Copies of a
collection — the documents and the embeddings both — pass directly
between the people who have them, so nobody re-does work that's already
been done, and the original source isn't asked for the same download
again and again.

Read these in order. Each one assumes the ones before it and nothing
after it.

| | document | answers |
|---|---|---|
| 1 | [What Louise is](01-louise.md) | why it exists, what a collection is, the three things you do with one |
| 2 | [Collections](02-collections.md) | what a collection is made of, how it is identified, how it changes over time |
| 3 | [Checking](03-checking.md) | how anyone verifies a copy, and how much to trust one they did not verify |
| 4 | [Sharing](04-sharing.md) | how copies and embedding work move between peers with no node in charge |
| 5 | [Asking](05-ask.md) | what a search does with what you hold |
| 6 | [Plugins](06-plugins.md) | how a new source becomes a collection |
| 7 | [Commands](07-commands.md) | the command line and the API, one vocabulary |

`notes/` holds the engineering record — measurements, postmortems,
scheduling — for people changing the code. Nothing there is needed to
understand or use the platform; everything there explains why some
detail is the way it is.
