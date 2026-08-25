"""A CLOSED pack is a dated snapshot: `pack build --until YYYY-MM-DD`
keeps the records that existed at the origin on that date and nothing
later (pack-format.md §6.1). Which date a record carries for that
decision is the plugin's:

    gutenberg   the PG release date (dcterms:issued) — a book enters the
                catalogue once and never leaves
    pubmed      DateRevised (fallback DateCompleted) — when the record
                last entered or changed in PubMed; a record revised after
                the cutoff is EXCLUDED, because the pack cannot hold the
                pre-revision text (the corpus holds the latest)
    wikipedia   none per record: a dump IS the snapshot, so the cutoff is
                the dump date, checked by the operator, recorded here
    embedded    none (a frozen published dataset)

Dates compare as normalised "YYYY-MM-DD" strings; a record with no date
under a plugin that has a rule is excluded and counted (`skipped`), never
silently kept — a snapshot must be reproducible from the rule alone.
"""
import re

_DATE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")

RULES = {
    "gutenberg": "issued <= until",
    "pubmed": "revised <= until",
    "wikipedia": "dump date <= until (no per-record filter)",
    "embedded": "dataset snapshot <= until (no per-record filter)",
}


def rule_for(plugin: str) -> str:
    return RULES.get(plugin, "no per-record filter")


def normalise(value) -> str | None:
    """'2026-3-7' -> '2026-03-07'; '2026' -> '2026-01-01'; junk -> None."""
    if not value:
        return None
    m = _DATE.match(str(value).strip())
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2) or "1", m.group(3) or "1"
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def record_key(plugin: str, record: dict) -> str | None:
    """The record's cutoff date, from the pack record (not the document)."""
    if plugin == "gutenberg":
        return normalise(record.get("date"))
    if plugin == "pubmed":
        extra = record.get("extra") or {}
        return normalise(extra.get("revised") or extra.get("date"))
    return None


def excluded(plugin: str, record: dict, until: str | None) -> str | None:
    """None to keep the record; else the reason it is outside the snapshot."""
    if not until:
        return None
    if plugin not in ("gutenberg", "pubmed"):
        return None
    key = record_key(plugin, record)
    if key is None:
        return f"no cutoff date ({rule_for(plugin)})"
    if key > until:
        return f"after cutoff: {key} > {until}"
    return None


def check_until(until: str | None) -> str | None:
    if until is None:
        return None
    norm = normalise(until)
    if norm is None or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(until)):
        raise ValueError(f"--until must be YYYY-MM-DD, got {until!r}")
    return norm
