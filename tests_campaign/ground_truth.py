"""What the node holds, read from its own data on 2026-10-20 (the gutenberg
manifest and gutenberg-book-editions' relations member, 2026-09 snapshot).
A test asserts against these, never against what a query happened to answer."""

#: gutenberg-book-editions' collection id (its Codex)
EDITIONS_ID = "sha256:182cd5f0717b1893a8c5e48af971ac3ccc459004600d2b16c142d7214dca7023"

#: well-known books: uid -> (lang, title as catalogued)
BOOKS = {
    "gutenberg:11": ("en", "Alice's Adventures in Wonderland"),
    "gutenberg:84": ("en", "Frankenstein; or, the modern prometheus"),
    "gutenberg:996": ("en", "Don Quixote"),
    "gutenberg:1000": ("it", "La Divina Commedia di Dante: Complete"),
    "gutenberg:1342": ("en", "Pride and Prejudice"),
    "gutenberg:1661": ("en", "The Adventures of Sherlock Holmes"),
    "gutenberg:2000": ("es", "Don Quijote"),
    "gutenberg:2600": ("en", "War and Peace"),
    "gutenberg:2701": ("en", "Moby Dick; Or, The Whale"),
    "gutenberg:5200": ("en", "Metamorphosis"),
}

#: same-work claims (annotations:same-work) whose object is the anchor: the
#: editions and translations gutenberg-book-editions joins to it
SAME_WORK = {
    "gutenberg:1342": {"gutenberg:20686", "gutenberg:20687", "gutenberg:26301", "gutenberg:42671",
                       "gutenberg:45186"},
    "gutenberg:11": {"gutenberg:17482", "gutenberg:19033", "gutenberg:19551", "gutenberg:19778",
                     "gutenberg:28371", "gutenberg:28885", "gutenberg:46569", "gutenberg:55456"},
    "gutenberg:5200": {"gutenberg:22367", "gutenberg:26298"},
}
#: the Spanish original and the English translation of Don Quixote are one work
DON_QUIXOTE = {"gutenberg:996", "gutenberg:2000"}
#: every relation in gutenberg-book-editions is same-work: no translation-of claim exists
RELATION_TYPES = {"annotations:same-work"}
