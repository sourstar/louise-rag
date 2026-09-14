# attic — boxed code, kept for reference

The operator's rule (2026-09-14): the old plugins are removed from the
active code and kept here, "for reference until they are either converted
to the final format or permanently dropped".

Nothing in this directory is imported, packaged in the image, deployed or
tested. It is the code as it stood when it was boxed, with the tests that
existed only for it; it will not run as it is, because the registry entries
and hooks that tied it into the platform were cut when it moved here.

| boxed | what it was | why boxed |
|---|---|---|
| `plugins/genome/`, `acquire/genome_spec.py` | reference genome assemblies (UCSC FASTA), DNA modality | no DNA calibration set exists, so no Codex can be written for it (docs/notes/migration-plan.md, step 8); the three genome collections stay parked in `config/collections-legacy/`, data untouched |
| `plugins/packed/` | a collection read back from a pre-Codex pack | superseded by the drag (02 §2.6): a collection's corpus is its records, built by `hold records build` |
| `pack/adopt.py` (`pack adopt`) | moved an origin collection's vectors into its packed twin | goes with `packed` |
| `tests/` | the tests that existed only for the above; `test_license_gate.py`, `test_license_gate_exchange.py`, `hold/test_credit_kinds.py` and `pack/test_build.py` are copies of live files trimmed or annotated down to their boxed part | — |

Converting one back means a Codex-shaped plugin under `app/plugins/`, its
registry entries, and its tests moved back under `tests/` and green.
