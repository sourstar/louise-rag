"""Hold — everything about keeping your own copy complete, current and
checked, with no other peer involved. docs/07-commands.md §7.1.

Modules mirror the command tree one to one: hold.collection, hold.records,
hold.snapshot, hold.embedder, hold.vectors, hold.index, hold.check, hold.key.
This package imports nothing heavy: a script that computes a Codex id must
not need a database, an encoder or a network."""
