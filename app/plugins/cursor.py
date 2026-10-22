"""Container scan cursor — incremental discovery for container plugins.

DiscoverState.state_dir (reserved by base.py's own docstring for exactly
this: "a PubMed baseline+updatefiles cursor") lets a plugin remember which
container files it has already enumerated, keyed by (mtime_ns, size). An
unchanged container is skipped WITHOUT opening it — at full-PubMed scale
that turns an hour of re-parsing ~1,270 × 16 MB XML files per scan into a
stat pass, and keeps discovery's in-memory ref buffer proportional to what
actually changed instead of to the whole corpus.

Safe by construction: the orchestrator (app/discovery.py) only upserts what
discover() yields and never prunes unseen documents, so skipping yields
nothing to un-know. The cursor lives beside the manifest, so wiping a
collection's data directory wipes its cursor with it.
"""
import json
import time
from pathlib import Path


def load(state_dir: Path | None, plugin: str) -> dict:
    if state_dir is None:
        return {}
    path = state_dir / f"{plugin}_scan_cursor.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


#: the third element of an entry recorded with its members — so a container
#: with NONE (a gutenberg transfer stub) is told from one recorded before
#: members were kept
MEMBERS = "members"


def signature(path: Path, members=None) -> list:
    """(mtime_ns, size) of `path`; with `members`, then MEMBERS and
    [name, mtime_ns, size] of each — the files inside a container directory
    that decide its record (gutenberg: pg<id>.rdf and the form file
    `pick_form` chose). A directory's mtime moves on a rename into it, which
    is how this platform lands every file, but NOT when a file is rewritten
    in place; the members' own stats see that (2026-10-22). A member gone is
    [name, None, None], which never equals what was recorded."""
    st = path.stat()
    sig = [st.st_mtime_ns, st.st_size]
    if members is None:
        return sig
    sig.append(MEMBERS)
    for name in members:
        try:
            m = (path / name).stat()
            sig.append([name, m.st_mtime_ns, m.st_size])
        except OSError:
            sig.append([name, None, None])
    return sig


def _members_of(entry) -> list[str] | None:
    """The member names an entry was recorded with; None for an entry
    recorded without the members rule."""
    if not isinstance(entry, list) or len(entry) < 3 or entry[2] != MEMBERS:
        return None
    return [m[0] for m in entry[3:]]


def key(path: Path, root: Path | None = None) -> str:
    """The cursor entry name for `path` — its path relative to the corpus
    root, not its bare file name.

    Keying on `path.name` is only safe while file names are unique across
    the whole corpus. That holds for pubmed (one flat directory of
    pubmed26nNNNN.xml.gz), for gutenberg (book directories) and for
    wikipedia (a dump and its index). It does NOT hold for the embedded
    plugin, whose format-v1 layout is data/{lang}/{NNN}.parquet: every
    language directory restarts the numbering, so data/de/000.parquet and
    data/tr/000.parquet shared one entry and each invalidated the other's
    signature. Nothing was ever skipped, and a ~437 MB shard was re-read on
    every scan — enough that the collection's discovery never finished.

    Relative rather than absolute, so that moving a corpus costs one
    re-examination instead of silently orphaning the whole cursor.

    A path outside the root falls back to its bare name; that is the old
    behaviour, and the callers that pass no root keep it.
    """
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.name


def unchanged(cursor: dict, path: Path, root: Path | None = None, *,
              members: bool = False) -> bool:
    """True when `path` is recorded with exactly this (mtime_ns, size).

    Accepts a LEGACY entry keyed on the bare file name so that moving to
    path keys does not discard a cursor built under the old scheme — on a
    large corpus that would mean one gratuitous full re-parse (pubmed: ~1,330
    containers). The fallback is safe because it still demands an exact
    signature match, so it can only fire for the very file that wrote it;
    two different shards would need identical mtime_ns AND size to collide.
    Legacy entries are pruned as each file is re-marked.

    `members`: the caller records each container with the files inside it
    that decide its record (`Progress(..., members=True)`); the recorded
    files are stat-ed again, and an entry recorded WITHOUT them is changed
    — a cursor written before members were kept is re-examined once.
    """
    k = key(path, root)
    entry = cursor.get(k)
    if members:
        named = _members_of(entry)
        return named is not None and entry == signature(path, named)
    sig = signature(path)
    if entry == sig:
        return True
    return k != path.name and cursor.get(path.name) == sig


def _record(cursor: dict, path: Path, root: Path | None, sig: list) -> None:
    k = key(path, root)
    cursor[k] = sig
    if k != path.name:
        cursor.pop(path.name, None)     # retire the legacy entry


def mark(cursor: dict, path: Path, root: Path | None = None) -> None:
    _record(cursor, path, root, signature(path))


def mark_if_settled(cursor: dict, path: Path, started_ns: int,
                    root: Path | None = None) -> bool:
    """Remember `path` only if it was last modified BEFORE this scan began.

    The timestamp race this closes: something lands in a directory while
    the scan is reading it (or within the filesystem's timestamp
    granularity of it). Marking then stores a signature that already
    includes the change, so the next scan sees "unchanged" and skips it —
    permanently. Refusing to mark costs one re-examination next scan and
    cannot lose an update.

    Matters most for DIRECTORIES, whose size rarely moves, so mtime is
    doing all the work; container files usually change size too, which is
    why the plain mark() has served them.

    Best effort, not a guarantee: directory timestamp granularity is
    coarse (measured in-container, a file created 5 ms after its parent
    left the parent's mtime untouched), so a change landing inside that
    window can still look older than the scan. What this rules out is the
    much wider window of a scan that takes minutes. The Library's "rescan"
    button clears the cursor for everything else."""
    sig = signature(path)
    if sig[0] < started_ns:
        _record(cursor, path, root, sig)
        return True
    return False


def save(state_dir: Path | None, plugin: str, cursor: dict) -> None:
    if state_dir is None:
        return
    path = state_dir / f"{plugin}_scan_cursor.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cursor), encoding="utf-8")
    tmp.replace(path)


class Progress:
    """Incremental, commit-safe cursor persistence for a streaming scan.

    Every plugin used to save its cursor once, at the END of discover(). A
    scan interrupted by a restart (worker restarts are frequent) therefore
    lost all of its progress and re-enumerated every container next time —
    observed live as wikivec spending 18 minutes per restart re-walking
    parquet shards whose documents were already in the manifest.

    Here a container is STAGED when the plugin finishes yielding it and
    PROMOTED into the saved cursor only once the orchestrator reports
    (DiscoverState.committed) that every ref up to and including that
    container's last one is durably in the manifest. The cursor can never
    get ahead of the manifest: after any interruption the next scan skips
    exactly the containers whose rows are committed, and re-enumerates the
    one that was in flight. Promotion happens on the orchestrator's commit
    hook (DiscoverState.on_commit), which it calls after every commit — so
    the cursor file is rewritten once per commit batch, not per ref.

    The settled-time guard (mark_if_settled) is applied at staging, with
    the signature captured THEN: a container that changes between staging
    and promotion is recorded with the signature the scan actually read.
    """

    def __init__(self, state, plugin: str, *, members: bool = False) -> None:
        self.state = state
        self.plugin = plugin
        self.members = members
        self.cursor = load(state.state_dir, plugin)
        self.started_ns = time.time_ns()
        self._staged: list[tuple[int, Path, list]] = []
        if state.on_commit is not None:
            state.on_commit.append(self.flush)

    def unchanged(self, path: Path) -> bool:
        return unchanged(self.cursor, path, self.state.corpus_root, members=self.members)

    def stage(self, path: Path, complete_at: int | None = None, members=()) -> None:
        """The plugin has yielded every ref of `path`. `complete_at` is the
        orchestrator's count of refs received when that happened — by
        default DiscoverState.received, which is exact when called right
        after the container's last yield resumed (the orchestrator has
        then taken exactly that many refs). `members`: under
        `Progress(..., members=True)`, the files inside `path` that decide
        its record, recorded with it (none for a stub)."""
        sig = signature(path, list(members) if self.members else None)
        if sig[0] >= self.started_ns or any(m[1] is None or m[1] >= self.started_ns
                                            for m in sig[3:]):
            return                      # modified during the scan: re-examine next time
        at = self.state.received if complete_at is None else complete_at
        self._staged.append((at, path, sig))

    def flush(self) -> None:
        """Promote every staged container whose last ref is committed, and
        save if anything was promoted. Called on the orchestrator's commit
        hook; safe to call any time."""
        committed = self.state.committed
        keep, promoted = [], False
        for at, path, sig in self._staged:
            if at <= committed:
                _record(self.cursor, path, self.state.corpus_root, sig)
                promoted = True
            else:
                keep.append((at, path, sig))
        self._staged = keep
        if promoted:
            save(self.state.state_dir, self.plugin, self.cursor)


def clear(state_dir: Path | None, plugin: str) -> bool:
    """Forget everything: the next scan re-examines the whole corpus.

    The cursor's signature is (mtime_ns, size) of the container or book
    directory, which catches every way this platform writes — acquisition
    always lands bytes with a rename into place, and a rename changes the
    directory. A file rewritten IN PLACE inside a book directory (an
    external mirror running `rsync --inplace`) is caught where the plugin
    records the directory's members (gutenberg since 2026-10-22); for any
    other plugin, and for anything else, this is the forced full rescan,
    wired to the Library's "rescan" button.

    EVERY cursor the plugin keeps: its own and each route's sub-cursor
    (`<plugin>-<route>_scan_cursor.json` — wikipedia walks its archive under
    `wikipedia-archive`). Clearing only the first is not "forget
    everything". Observed live 2026-09-15: wikipedia's origin moved, this
    cleared the dump cursor and left the archive's; the archive walk then
    skipped every shard as unchanged (the keys are corpus-relative), no
    archive locator was ever repaired, and the archive import — which finds
    its shards from those locators — stood still."""
    if state_dir is None:
        return False
    cleared = False
    for path in (state_dir / f"{plugin}_scan_cursor.json",
                 *state_dir.glob(f"{plugin}-*_scan_cursor.json")):
        try:
            path.unlink()
            cleared = True
        except OSError:          # already gone, or unremovable: nothing to forget
            pass
    return cleared
