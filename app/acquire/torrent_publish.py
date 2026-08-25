"""Publishing a corpus as a torrent — the seeding half of §5's raw plane.

A node that only downloads is a leech, and a swarm of leeches moves no
bytes. This is what turns a corpus directory this node already holds into
something another node can ask for: a .torrent describing it, a magnet URI
naming it, and a seeding handle seeded from the files already on disk.

BITTORRENT v2 (BEP 52) by default, and the reason is the mutable-source
case rather than novelty. v2 hashes each file into its own merkle tree, so
two snapshots of the same corpus taken a month apart share every file that
did not change — a peer who holds last month's snapshot fetches only the
difference. A v1 torrent hashes a single chain across the whole payload and
one changed byte early on invalidates everything after it. `hybrid=True`
publishes both formats in one torrent so v1-only clients still work.

WHAT IS NOT HERE. No tracker is written into the torrent by default. A
tracker is a coordination service someone has to run and trust, and the
platform already has two ways for peers to find each other without one
(docs/p2p-discovery.md): an explicit peer list, and the DHT once an
infohash is known. `--tracker` exists for anyone who wants one anyway.
"""
import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .spec import TransportError
from .transport_torrent import _lt, content_files

# 16 MiB. Large corpora are the case that matters: PubMed's baseline is
# ~1,270 containers of ~16 MB, and a small piece size on a 200 GB payload
# produces a metadata blob so large that exchanging it costs more than it
# saves. libtorrent will not object to any power of two.
DEFAULT_PIECE_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True)
class PublishedTorrent:
    """What a publisher hands to a peer, and what the catalogue records."""
    name: str
    torrent_path: Path
    magnet: str
    info_hash: str
    files: int
    total_bytes: int

    def as_catalog_entry(self) -> dict:
        return {"name": self.name, "magnet": self.magnet,
                "info_hash": self.info_hash, "files": self.files,
                "bytes": self.total_bytes}


def publish_corpus(corpus_dir: Path, out_path: Path, *, name: str | None = None,
                   piece_size: int = DEFAULT_PIECE_SIZE,
                   trackers: tuple[str, ...] = (),
                   web_seeds: tuple[str, ...] = (),
                   comment: str | None = None,
                   hybrid: bool = True,
                   exclude_patterns: tuple[str, ...] = (),
                   include_patterns: tuple[str, ...] = ()) -> PublishedTorrent:
    """Hash `corpus_dir` into a .torrent at `out_path`.

    Hashing reads every byte, so this is minutes of disk on a large corpus
    and is deliberately a separate, explicit command rather than something
    the worker does on a timer.

    `web_seeds` (BEP 19) are worth passing whenever the corpus also exists
    at a public URL: a peer can then complete the download at origin speed
    with nobody seeding at all, which is the cold-start problem that
    otherwise makes a new swarm useless.
    """
    lt = _lt()
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.is_dir():
        raise TransportError(f"{corpus_dir}: not a directory")

    fs = lt.file_storage()
    patterns = tuple(exclude_patterns or ())

    # POLICY (spec.extras): peers exchange only the data that fulfils
    # Louise's purpose. Opted-in extra formats live in the SAME corpus
    # tree (gutenberg: the epub beside the txt), so publishing the
    # directory wholesale would ship them — exclude by pattern here, at
    # the one place corpus bytes become p2p material. HIDDEN entries are
    # never published either: a plugin's derived caches live in a hidden
    # directory beside the archive (pubmed's `.pack/`), are rebuilt from
    # it, and must not become p2p material or change a snapshot's identity.
    # `include_patterns` (STATUS 5h) publishes a SUBSET of the tree under
    # the SAME root — e.g. one language of the pre-embedded corpus
    # ("*/data/en/*" under root `data`), so a peer's files land in the
    # layout the plugin expects and several such torrents combine.
    includes = tuple(include_patterns or ())

    def _include(path: str) -> bool:
        rel = Path(path).as_posix()
        if _hidden(rel):
            return False
        # libtorrent asks about DIRECTORIES too, and a False on one prunes
        # its whole subtree — so a directory passes on the hidden check
        # alone; include patterns judge files
        if Path(path).is_dir():
            return True
        base = rel.rsplit("/", 1)[-1]
        if includes and not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat)
                                for pat in includes):
            return False
        return not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat)
                       for pat in patterns)
    lt.add_files(fs, str(corpus_dir), _include)
    if fs.num_files() == 0:
        raise TransportError(f"{corpus_dir}: nothing to publish (no files)")

    # libtorrent 2.x emits a HYBRID torrent by default — v2 merkle trees
    # plus a v1 chain — which is what we want: v2 for the per-file hashing
    # that makes snapshots share unchanged files, v1 so older clients are
    # not excluded from a network whose whole point is reach. hybrid=False
    # asks for v2 only, which is smaller but shuts those clients out.
    creator = (lt.create_torrent(fs, piece_size=piece_size) if hybrid
               else lt.create_torrent(fs, piece_size=piece_size,
                                       flags=lt.create_torrent.v2_only))
    for url in trackers:
        creator.add_tracker(url)
    for url in web_seeds:
        creator.add_url_seed(url)
    creator.set_creator("louise-rag")
    if comment:
        creator.set_comment(comment)

    # Reads every byte of the corpus. The parent of corpus_dir is the base:
    # add_files() recorded paths relative to it, and the hashes must be
    # computed against exactly those paths.
    lt.set_piece_hashes(creator, str(corpus_dir.parent))

    entry = creator.generate()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(lt.bencode(entry))

    info = lt.torrent_info(str(out_path))
    # Counted from the CONTENT files only. Alignment padding is real in the
    # torrent and irrelevant to a human: reporting "4 files / 32,768 bytes"
    # for a two-file, 12,000-byte corpus is simply wrong, and it is the
    # number an operator checks against what they think they published.
    content = content_files(info)
    return PublishedTorrent(
        name=name or corpus_dir.name,
        torrent_path=out_path,
        magnet=lt.make_magnet_uri(info),
        info_hash=_info_hash(info),
        files=len(content),
        total_bytes=sum(size for _, _, size in content),
    )


def _info_hash(info) -> str:
    """The torrent's identity, preferring v2's SHA-256 when present.

    Reported as a plain hex string because it is what a peer quotes back:
    it goes in the catalogue, in a magnet, and in a support conversation.
    """
    try:
        hashes = info.info_hashes()
        if hashes.has_v2():
            return str(hashes.v2)
        if hashes.has_v1():
            return str(hashes.v1)
    except Exception:   # noqa: BLE001 — older bindings expose only the v1 hash
        pass
    return str(info.info_hash())


def seed(torrent_path: Path, corpus_dir: Path):
    """Start serving an already-published torrent from the files on disk.

    The handle joins the process-global session, so it keeps seeding for as
    long as the worker runs. libtorrent re-checks the pieces against the
    corpus first, which is also a free integrity audit of what this node
    claims to hold.
    """
    from .transport_torrent import get_session

    lt = _lt()
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent_path))
    params.save_path = str(Path(corpus_dir).parent)
    params.flags |= lt.torrent_flags.seed_mode
    return get_session().add_torrent(params)


def corpus_digest(corpus_dir: Path) -> str:
    """A cheap identity for "which snapshot is this" — sorted (path, size,
    mtime) rather than content, so it costs a stat per file instead of a
    full read. Enough to notice that a corpus changed; never enough to
    prove two corpora are identical, which is what the torrent's own hashes
    are for."""
    h = hashlib.blake2b(digest_size=16)
    for p in sorted(Path(corpus_dir).rglob("*")):
        rel = p.relative_to(corpus_dir).as_posix()
        if p.is_file() and not _hidden(rel):
            st = p.stat()
            h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def _hidden(rel_posix: str) -> bool:
    """Any path component starting with '.': derived caches (pubmed's
    `.pack/`), editor droppings — never corpus material."""
    return any(part.startswith(".") for part in rel_posix.split("/") if part)
