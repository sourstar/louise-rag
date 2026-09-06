"""PackedSource — CorpusSource over a PACK: record shards written by
`pack build` (app/pack/records.py), plus the manifest.

A collection created over a pack needs no origin files and no parser: the
records ARE the parsed documents. Everything that gives the pack its
value as a *derived* corpus follows from three identities being kept:

  - the document's UID is the record's uid, so `source_id` is the ORIGIN
    plugin's (a pubmed pack yields `pubmed:<pmid>`): bundles, the genesis
    archive (`uid_prefix`) and a derived collection all key on it;
  - `raw_hash` is the record's ORIGIN hash (the origin bytes' hash the
    initiator recorded), so possession hashes agree with a node that
    indexed the origin;
  - the chunker is the origin plugin's (or the pack's declared layers), so
    chunk ids are identical — vectors embedded from the origin apply to the
    pack and vice versa.

Locator (plugin-opaque, survives the manifest):  "<shard path>::<frame>::<line>"
"""
import json
import threading
from pathlib import Path

from .. import cursor
from ..base import (CanonicalDoc, DiscoverState, DocRef, LicenseClass, Mutability,
                    PrivacyClass, RawDoc)
from ...pack import manifest as manifest_mod, records

_LOCATOR_SEP = "::"
_LICENSE = {"green": LicenseClass.GREEN, "yellow": LicenseClass.YELLOW, "red": LicenseClass.RED}


def split_locator(locator: str) -> tuple[Path, int, int]:
    head, _, line = locator.rpartition(_LOCATOR_SEP)
    path, _, frame = head.rpartition(_LOCATOR_SEP)
    return Path(path), int(frame), int(line)


class PackedSource:
    """The registry's placeholder instance describes the plugin in the
    abstract; a collection gets a per-pack instance from `for_pack`, whose
    identity fields come from the pack's manifest."""
    source_id = "packed"
    license_class = LicenseClass.YELLOW        # the safe default; the pack says
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "book"
    mutability = Mutability.IMMUTABLE

    def __init__(self, pack_dir: Path | None = None, manifest: dict | None = None):
        self.pack_dir = Path(pack_dir) if pack_dir else None
        self.manifest = manifest
        self.layers = None
        if manifest is not None:
            origin = manifest.get("plugin") or "packed"
            self.source_id = origin
            self.license_class = _LICENSE.get((manifest.get("license") or {}).get("class"),
                                              LicenseClass.YELLOW)
            # the origin plugin's chunking identity, so chunk ids match it
            from ...collections import PLUGIN_REGISTRY
            base = PLUGIN_REGISTRY.get(origin)
            if base is not None:
                self.chunking_profile = getattr(base, "chunking_profile", "book")
                self.mutability = getattr(base, "mutability", Mutability.IMMUTABLE)
            self.layers = manifest.get("layers") or None

    @property
    def terms_path(self) -> Path | None:
        if self.pack_dir is None:
            return None
        p = self.pack_dir / "TERMS.txt"
        return p if p.is_file() else None

    # --- CorpusSource ---

    def discover(self, state: DiscoverState):
        root = Path(state.corpus_root)
        rec_dir = root / "records"
        if not rec_dir.is_dir():
            return
        progress = cursor.Progress(state, self.source_id)
        for shard in sorted(p for p in rec_dir.rglob(f"*{records.SUFFIX}") if p.is_file()):
            if progress.unchanged(shard):
                continue
            st = shard.stat()
            try:
                index = records.cached_index(shard)
            except Exception as e:   # noqa: BLE001 — one container error, never the scan
                yield DocRef(source_id=self.source_id, native_id=shard.name, locator=str(shard),
                             scope="container",
                             error=f"unreadable shard {shard.relative_to(root)}: "
                                   f"{type(e).__name__}: {e}"[:200])
                continue
            for uid, (frame, line) in sorted(index.uids.items(), key=lambda kv: (kv[1][0], kv[1][1])):
                prefix, _, native_id = uid.partition(":")
                if not native_id:
                    continue
                yield DocRef(source_id=self.source_id, native_id=native_id,
                             locator=f"{shard}{_LOCATOR_SEP}{frame}{_LOCATOR_SEP}{line}",
                             mtime=int(st.st_mtime), size=st.st_size, extra={"uid": uid})
            progress.stage(shard)
        progress.flush()

    def fetch(self, ref: DocRef) -> RawDoc:
        shard, frame, line = split_locator(ref.locator)
        record = records.read_record_at(shard, frame, line)
        data = records.canonical_line(record)
        # the ORIGIN hash: possession agrees with nodes that hold the origin
        return RawDoc(ref=ref, raw_hash=record.get("origin_hash") or record["content_hash"], data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        record = json.loads(raw.data.decode("utf-8"))
        extra = dict(record.get("extra") or {})
        extra["pack_layer"] = int(record.get("layer") or 0)
        return CanonicalDoc(
            uid=record["uid"], title=record.get("title") or "",
            creators=list(record.get("creators") or []), language=record.get("lang") or "und",
            date=record.get("date"), subjects=list(record.get("subjects") or []),
            keys=list(record.get("keys") or []), extra=extra,
            sections=[(s[0], s[1]) for s in record.get("sections") or []],
            raw_hash=raw.raw_hash)

    def record(self, ref_locator: str) -> dict:
        shard, frame, line = split_locator(ref_locator)
        return records.read_record_at(shard, frame, line)


_instances: dict[str, tuple[int, PackedSource]] = {}
_instances_lock = threading.Lock()


def for_pack(pack_dir: Path) -> PackedSource:
    """The per-pack instance (cached per directory, refreshed when the
    manifest changes). A directory without a manifest yields the abstract
    placeholder: discovery then finds nothing, which is the truthful state."""
    pack_dir = Path(pack_dir)
    mpath = pack_dir / manifest_mod.FILENAME
    key = str(pack_dir)
    mtime = mpath.stat().st_mtime_ns if mpath.is_file() else -1
    with _instances_lock:
        hit = _instances.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    manifest = None
    if mtime >= 0:
        try:
            manifest = manifest_mod.load(pack_dir)
        except manifest_mod.ManifestError:
            manifest = None
    inst = PackedSource(pack_dir, manifest)
    with _instances_lock:
        _instances[key] = (mtime, inst)
    return inst
