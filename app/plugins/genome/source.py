"""GenomeSource — CorpusSource over a reference assembly FASTA
(architecture-v2.md §9).

One collection per assembly (the doc's rule): the collection's corpus_dir
holds a single `<assembly>.fa[.gz]`, each chromosome/contig is one document,
and the generic chunker's fixed char windows ARE §9's deterministic
overlapping sequence windows (CHUNK_TOKENS*4 bases, CHUNK_OVERLAP*4
overlap) — a chunk id like `genome:3:17:1` maps to a coordinate as
`window_start = seq * (CHUNK_TOKENS-CHUNK_OVERLAP) * 4` on that contig.

Multi-species is free by construction: yeast (12 Mbp) and fly (140 Mbp)
validate the plugin in GPU-minutes before anyone spends human-scale hours,
and Evo-class DNA encoders are trained across all domains of life, so one
space serves every assembly. The dense leg activates whenever the
collection's [embed] recipe points at a DNA encoder; until then the
catalog/lookup legs work alone (§9: multi-modal, merged only where merging
is meaningful).

Locator: "<fasta path>::<record ordinal>" — plugin-opaque, survives the
manifest (§2), same convention as pubmed/wikipedia.
"""
import gzip
import hashlib
from pathlib import Path

from .. import cursor
from ..base import CanonicalDoc, DiscoverState, DocRef, LicenseClass, Mutability, PrivacyClass, RawDoc

_LOCATOR_SEP = "::"
_FASTA_SUFFIXES = (".fa", ".fa.gz", ".fasta", ".fasta.gz", ".fna", ".fna.gz")


def split_locator(locator: str) -> tuple[Path, int]:
    path, _, ordinal = locator.rpartition(_LOCATOR_SEP)
    return Path(path), int(ordinal)


def _assembly_of(path: Path) -> str:
    """The assembly a FASTA belongs to. UCSC's layout is
    <assembly>/bigZips/<assembly>.fa.gz, and the file is named after the
    assembly either way, so the stem is the reliable signal."""
    name = path.name
    for suffix in _FASTA_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _open(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="ascii", errors="replace")
    return open(path, "rt", encoding="ascii", errors="replace")


def _records(path: Path):
    """Yields (ordinal, name, sequence) streaming — one FASTA record each."""
    ordinal = -1
    name = None
    parts: list[str] = []
    with _open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith(">"):
                if name is not None:
                    yield ordinal, name, "".join(parts)
                ordinal += 1
                name = line[1:].split()[0] if len(line) > 1 else f"contig{ordinal}"
                parts = []
            elif name is not None:
                parts.append(line.upper())
    if name is not None:
        yield ordinal, name, "".join(parts)


class GenomeSource:
    source_id = "genome"
    license_class = LicenseClass.GREEN     # consortium reference assemblies
    privacy_class = PrivacyClass.PUBLIC    # REFERENCE genomes only — §8's
                                            # never-share rule is about personal
                                            # genomes, which this plugin does
                                            # not ingest
    chunking_profile = "sequence-window"
    # A reference assembly is frozen by definition; a new assembly is
    # a new collection (§9). The windows ARE coordinates, and moving
    # them would break the chunk-id-to-locus mapping.
    mutability = Mutability.IMMUTABLE

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        fastas = sorted(p for p in root.rglob("*")
                        if p.is_file() and p.name.endswith(_FASTA_SUFFIXES))
        # ONE ASSEMBLY PER COLLECTION (§9), enforced here rather than
        # trusted: native_id is the record ordinal within a FASTA, so a
        # second assembly mints the SAME document ids and overwrites the
        # first. Observed live — a `full` grow put dm6 and hg38 beside
        # sacCer3 and left yeast titles on human locators with yeast
        # vectors still attached. Extra assemblies are refused as container
        # reports (never documents) so the operator sees exactly what to do.
        if len(fastas) > 1:
            primary = _assembly_of(fastas[0])
            foreign = [p for p in fastas if _assembly_of(p) != primary]
            if foreign:
                for p in foreign:
                    yield DocRef(
                        source_id=self.source_id, native_id=p.name,
                        locator=str(p), scope="container",
                        error=f"refusing {_assembly_of(p)}: this collection "
                              f"already holds {primary}, and one collection "
                              f"holds exactly one assembly (§9) — give "
                              f"{_assembly_of(p)} its own collection and "
                              f"corpus folder")
                fastas = [p for p in fastas if _assembly_of(p) == primary]
        progress = cursor.Progress(state, self.source_id)
        for fasta in fastas:
            if progress.unchanged(fasta):
                continue     # already enumerated at this (mtime, size)
            st = fasta.stat()
            try:
                for ordinal, name, seq in _records(fasta):
                    if not seq:
                        continue
                    yield DocRef(
                        source_id=self.source_id, native_id=str(ordinal),
                        locator=f"{fasta}{_LOCATOR_SEP}{ordinal}",
                        mtime=int(st.st_mtime), size=len(seq),
                        extra={"contig": name, "assembly": fasta.name})
            except Exception as e:  # noqa: BLE001 — truncated/corrupt FASTA:
                # one error ref, never a dead scan
                yield DocRef(source_id=self.source_id, native_id=fasta.name,
                             locator=str(fasta), scope="container",
                             error=f"unreadable FASTA {fasta.name}: "
                                    f"{type(e).__name__}: {e}"[:200])
                continue
            progress.stage(fasta)      # recorded once its refs are committed
        progress.flush()

    def fetch(self, ref: DocRef) -> RawDoc:
        fasta, ordinal = split_locator(ref.locator)
        for i, name, seq in _records(fasta):
            if i == ordinal:
                data = f"{name}\n{seq}".encode("ascii", errors="replace")
                digest = hashlib.blake2b(data, digest_size=16).hexdigest()
                return RawDoc(ref=ref, raw_hash=digest, data=data)
        raise RuntimeError(f"record {ordinal} not found in {fasta}")

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        name, _, seq = raw.data.decode("ascii").partition("\n")
        fasta, _ = split_locator(raw.ref.locator)
        assembly = fasta.name
        for suffix in _FASTA_SUFFIXES:
            if assembly.endswith(suffix):
                assembly = assembly[: -len(suffix)]
                break
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}",
            title=f"{assembly} {name}",
            creators=[],
            language="dna",            # no stemmer applies; generic analyzer
            date=None,
            subjects=[assembly],
            keys=[f"{assembly}:{name}"],   # species-qualified coordinate key (§9)
            extra={"assembly": assembly, "contig": name, "length": len(seq)},
            sections=[(name, seq)],
            raw_hash=raw.raw_hash,
        )
