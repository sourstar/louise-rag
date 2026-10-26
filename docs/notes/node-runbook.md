# Node runbook — operating München

For a person who has never seen the node. It says what runs where, how to start, stop,
update and measure it, and what to do when the usual things go wrong. Addresses,
ports beyond the defaults, the client machine and other node-specific facts live in
`deploy/reference/DEPLOYMENT.md` (gitignored, on the operator's machine): this note
points there and never copies them. **Secrets are named by path only, never by value.**

Read first: `CLAUDE.md` (the rules), `docs/notes/STATUS.md` (where things stand),
`docs/notes/scheduling.md` (what runs when), `docs/notes/measuring.md` (how to measure).

## 1. The machine

- **TrueNAS SCALE** host, 12 cpus, 61 GB RAM, ZFS ARC capped at 16 GB.
- **GPU**: Intel Arc Pro B70, 32 GB VRAM, xe driver, at PCI `0000:03:00.0`. The
  iGPU is `0000:13:00.0`. `/dev/dri/cardN` numbers can swap across reboots (§12):
  always check by PCI address.
- **Pools**:
  - `ssd4` (NVMe, **not redundant**): Louise's app and data (`/mnt/ssd4/apps/louise-rag`);
  - `ssd5` (**not redundant**): LocalAI's models and backends (`/mnt/ssd5/apps/localai`);
  - `tank` (ZFS, redundant): corpora (`louise-corpora` → `/corpora`, Project Gutenberg's
    mirror → `/corpora-custom/gutenberg`), SHARDs, backups, scratch.
- **Reach**: ssh as root with the operator's key (path in DEPLOYMENT.md); the root shell
  is **zsh**, so run multi-line logic as `bash -s` or as a script file. Tailscale gives
  an address off the LAN (DEPLOYMENT.md).
- **Clock**: Europe/Rome, and the apps carry `TZ=Europe/Rome`. DST changes shift
  `date -d` targets; a sleeper computed before a change still fires at the right
  absolute time.

## 2. Apps (TrueNAS) and containers

**NEVER delete a TrueNAS app** (operator's standing order). Stop it, or leave it.

| app | containers | what |
|---|---|---|
| `louise-rag` | `ix-louise-rag-web-1` (port 30042: Ask, Library, System, API), `ix-louise-rag-worker-1` (the pipelines, share, maintainers), `ix-louise-rag-gpu-probe-1` | Louise; both run the image `louise-rag:local` built from `/mnt/ssd4/apps/louise-rag/src` |
| `louise-gpu-governor` | `ix-louise-gpu-governor-gpu-governor-1` | holds the card's clock up while a person waits (§9). Its own app by design: an optional helper must not share an app with what it helps |
| `louise-vega` | `ix-louise-vega-vega-1` | VeGa, the receipts wrapper in front of LocalAI's encoders (docs/notes/vega.md) |
| `localai` | `ix-localai-localai-1` (port 30286) | every model: encoders, rerankers, translator, chat. **The sole owner of VRAM** |
| others | jellyfin, tailscale, cloudflared-*, localagi; stopped: immich, nextcloud, … | not Louise's; leave them alone |

Start or stop: `midclt call app.stop louise-rag` / `app.start` (a job; poll
`core.get_jobs` until SUCCESS). `docker restart` is not the way: TrueNAS recreates
containers from its compose config.

## 3. Where things are

- **Code**: `/mnt/ssd4/apps/louise-rag/src` (the deployed tree; `.deployed-commit` names
  it); `src.old/` is the previous one until the next deploy.
- **Config** (`/config` in the containers):
  - `collections/<name>.toml`: desired state per collection;
  - `platform.toml`: the node, its peers, budgets;
  - `recipes/`.
- **Data** (`/data`):
  - `collections/<name>/state/manifest.sqlite`: SQLite WAL; never VACUUM a manifest;
  - `collections/<name>/index/`: the text indexes;
  - `collections/<name>/lance/`: the vectors;
  - `collections/<name>/state/build/`: a staged records build;
  - `state/node.sqlite`: the node's mode, claims, peers, governor heartbeat, GPU samples;
  - `cache/` = `LOUISE_CACHE_DIR`: derived and re-creatable, e.g. the wikidata pack;
  - `refs.db`: the reference sources.
- **Corpora**: `/corpora/<collection>` (tank), gutenberg's mirror `/corpora-custom/gutenberg`.
- **SHARDs**: `/shards/<collection>`; bundles: `/bundles/<collection>`.
- **LocalAI**:
  - model YAMLs: `/mnt/ssd5/apps/localai/models/*.yaml`;
  - weights: under `models/weights/`;
  - staged copies in the repo: `deploy/vega/stage/models/` (deployed = staged; keep them equal).
- **Node scripts**: `/root/` and `/root/louise-scheduled/`. Their committed copies are
  `deploy/scheduled/` (cron and window scripts) and `deploy/16f/` (deploy scripts).

## 4. Secrets (by path)

- **Node key**: `NODE_KEY_PATH` = `/config/node.key` (it moves to a secrets dataset
  mounted on the worker alone, read-only, in the next deploy; the `/config` copy is
  then moved to `/mnt/ssd4/apps/louise-rag/secrets-moved/`, mounted nowhere). It
  signs the node's digests and SHARDs, so losing it means a new identity for every
  peer. Public half: `ed25519:ba2168c0…`.
  - **Backup on the node** (2026-10-25, made by the operator, checked
    byte-identical): `/mnt/tank/homes/alanari/louise-node.key` (0400, alanari) with
    `louise-node.pub` (0444) beside it, the public half derived from that copy. It
    is on tank, so it covers a lost ssd4 dataset, but it is on the SAME machine.
  - **Off the machine** (password manager, paper or USB): the common practice,
    for the operator to keep.
- **Tokens**: `DEPOSIT_TOKEN_FILE` (path). `ADMIN_TOKEN` is today an env VALUE in the
  app's compose config, against invariant 8 (open item, §13).
- **LocalAI**: its `/api/settings` JSON carries `api_keys` and a p2p token; never print it.
- **The ssh key** sits on the operator's client (DEPLOYMENT.md).
- **Never print** the TrueNAS app config (`midclt call app.config louise-rag`): it holds
  the env.

## 5. Updating Louise (the deploy)

ONE change per deploy wherever a measurement is pending, so its numbers stay
attributable.

1. **Gate, on the client**: the full suite, serially, in the gate image, on the exact tree:
   `docker run --rm -v <tree>:/srv -w /srv -e PYTHONPATH=/srv -e PYTHONDONTWRITEBYTECODE=1 louise-rag:gate timeout -s KILL 2700 python -m pytest -q -p no:cacheprovider`
   (~20 min; no `-n`). A red test blocks the deploy.
2. **Archive** the commit:
   `git archive --format=tar.gz <sha> app tests tools docs calibration deploy requirements.txt requirements-dev.txt constraints.txt Dockerfile platform.toml.example`
   → the node's `/tmp/louise-src.tgz`, and `<sha>` → `/tmp/louise-commit`.
3. **Script**: derive `/root/deploy-louise-<sha>.sh` from the last one
   (`deploy/16f/deploy-louise-888a9be.sh`). Add `grep -q` markers that prove the new
   code is in the staged tree, and the new tests to its node test run.
4. **Run** `bash /root/deploy-louise-<sha>.sh`. It:
   - stages the tree beside the live one;
   - builds `louise-rag:staged` and checks its `pip freeze` equals `constraints.txt`;
   - loads every collection under the staged code and requires no refusal;
   - runs the named tests;
   - promotes (`src` → `src.old`), builds `louise-rag:local` and tags `louise-rag:pre-<sha>`;
   - saves both containers' logs to `/root/louise-logs/`;
   - stops the app (waits for the job), sets `LOUISE_PLATFORM_IMAGE` and starts it;
   - checks both containers run the new image and `GET /ask` answers 200.
5. **The way back**: `louise-rag:pre-<sha>` and `src.old/`.

A **records build** runs in its own container (§7) and may mount a NEWER tree than
the deployed one (`-v <tree>:/srv:ro`). Do that only after an A/B proves its parts
byte-identical (`tools/measure/records_ab.py`).

## 6. Modes, pauses, cron

- **The node's mode**: paused / acquire-only / seed-only / running. It survives a
  restart by design (CLAUDE.md invariant 10). Read it with
  `docker exec ix-louise-rag-worker-1 python -m app.louise node status`.
- **One collection**: `… node pause <collection>` / `node resume <collection>`;
  wrapped by `deploy/scheduled/set-pause.sh <collection> pause|resume`.
- **Embedding mode**: `set-embed.sh <collection> <from> <to>` rewrites one line of
  the TOML and applies it.
- **Cron jobs** (TrueNAS, `midclt call cronjob.query`):

| id | when | what |
|---|---|---|
| 13 / 14 | 06:00 / 22:30 daily | normattiva pause / resume |
| 18 | 06:00, 12:00, 17:00 Mon–Fri | wikipedia-v8 build stop |
| 19 | 08:00 Mon–Fri | wikipedia-v8 build start |
| 20 | 13:30, 22:30 Mon–Fri | wikipedia-v8 build start |
| 21 | 00:05 Sat, Sun | wikipedia-v8 build start (idempotent) |
| 6 / 5 | 22:30 / 06:00 daily | gutenberg embed on-demand → all + GPU clock capped / all → on-demand + clock restored |
| 7–12 | — | DISABLED, kept: earlier pubmed/wikidata/contribute windows |

- **Quiet hours** (the operator's, 2026-10-26; the NAS is where people live):
  **22:30–06:00: GPU light (max clock capped), CPU per the windows (build ≤ 8 cores);
  by day, short GPU probes only.**
  - "GPU light": `deploy/scheduled/gpu-cap.sh on` at 22:30 writes the B70's
    `max_freq` down to half its maximum (rp0/2 = 1400 MHz), `off` at 06:00 gives it
    back (cron 6 / cron 5, beside gutenberg's embed switch). A clock changes how fast
    a vector comes, never its bytes (invariant 12 untouched). The governor's hold
    (`HOLD_FRACTION` 0.5 of rp0) equals the cap, never above it. Log:
    `/root/louise-scheduled/gpu-cap.log`.
  - The build: `wk-build-step.sh` `CPUS=8` (`PARSE_PROCESSES` 8).
  - A short GPU probe by day: < 5 min, at most one an hour.
- **One-off timed work**: `deploy/scheduled/in-window.sh <END> <script> <log>` runs a
  resumable script and SIGTERMs only its own containers at END (label
  `louise.in-window=<id>`).

## 7. The records build container (wikipedia-v8)

- `deploy/scheduled/wikipedia-build.sh start|stop` (log: `/root/louise-scheduled/wikipedia-build.log`).
  - **start**: does nothing if it is running, or if `/root/hold-wikipedia-build` exists
    (the file says why). Until the deployed tree carries the parse-pool fixes
    (`gh-115634` in `app/parallel.py`), it mounts the tree `/root/louise-build-src`
    points at (a symlink).
  - **stop**: SIGTERM; the build finishes its part, commits, exits; after 15 min it
    SIGKILLs. Safe: the parts are staged and resumable.
- `deploy/scheduled/wk-build-step.sh` runs the container:
  - `--cpus 8`, `PARSE_PROCESSES` 8 (the operator, 2026-10-26), 24 GB, nice 19;
  - output to `/root/wikipedia-v8/build-night.log`.
- **Progress**: the staged build database
  `data/collections/wikipedia-v8/state/build/.build/build.db`. The table `shards`
  gives records, chunks and `written_at` per part; the table `plan` gives the total.
  `deploy/scheduled/build-children.sh` samples the pool's children and cpu every 60 s.
- **A stopped build promotes nothing.** The snapshot is created after the build ends:
  `hold snapshot create`, then `hold check`.

## 8. LocalAI and VeGa

- **Models** are YAMLs in LocalAI's models directory. Encoders carry their vendor's
  bitwise-determinism env (`GGML_SYCL_ENABLE_DNN=0` on the Arc;
  `docs/notes/approved-encoders.md`). A new engine is measured bit-exact before it
  embeds (invariant 12).
- **API** (on LocalAI's port):
  - `GET /system`: loaded models with pids;
  - `PUT /models/toggle-pinned/<m>/pin|unpin`;
  - `POST /backend/shutdown {"model": …}`: unloads a model;
  - `/v1/rerank`, `/v1/embeddings`.
  - VeGa requires a tag on every encoder input (`app/vega/client.tag_batch`); an
    untagged `/v1/embeddings` call is refused.
- **WatchDog**: memory threshold 0.85 (`deploy/vega/stage/localai_settings.py`, idempotent).
  Above it LocalAI evicts the least recently used model.
- **Pins**: nothing is pinned statically. Louise pins a model for the duration of its
  use and restores the prior state (`app/model_pins.py`); failed pin calls show in the
  System tab.
- **Priority**: query steps outrank corpus embedding (`app/dispatch.py` levels).
  Corpus work stands aside on the whole card while a person's query step holds the
  claim.

## 9. The GPU governor

- **What**: while a query holds the node claim, it sets the card's `min_freq` to half
  its maximum (`app/gpu_clock.py` `HOLD_FRACTION`), and gives it back at the claim's end.
- **Install**: from `deploy/truenas-gpu-governor.yaml`; it binds
  `/sys/class/drm/card0/device/tile0/gt0/freq0`. Check `card0` is `0000:03:00.0` first.
- **Health**: its heartbeat is `control.gpu_governor` in `node.sqlite`, refreshed every
  few seconds; the System tab's GPU fragment shows it.
- **Restart**: a crash restarts it. A `docker kill` counts as a manual stop, so bring
  it back with `midclt call app.start louise-gpu-governor`.

## 10. Backups

- **Snapshots**: task 3 takes `ssd4` recursively every hour and keeps 2 hours; task 2
  takes `ssd5` daily at 01:00 and keeps a week.
- **Replication**: 5 copies `ssd4` → `tank/backup/ssd4`, 4 copies `ssd5` →
  `tank/backup/ssd5`, both local push. These replications are the ONLY backup of the
  two non-redundant pools: check them after any intervention
  (`midclt call replication.query`, state FINISHED).
- **Never make a ZFS clone inside ssd4 or ssd5**: the recursive snapshot task picks
  it up and replicates it in full (it happened, 2026-10-23). Measurement scratch goes
  on tank (`/mnt/tank/scratch-*`), which is redundant and not replicated.
- **The node key**: an offline copy (§4). The SHARDs on tank are re-creatable from the
  corpora; the manifests are re-creatable only at great cost (days of CPU), so the
  replication matters.

## 11. Logs

- **Containers**: `docker logs ix-louise-rag-worker-1` / `ix-louise-rag-web-1`; every
  line names its collection. A deploy recreates them, but the script saves the old
  logs to `/root/louise-logs/`.
- **LocalAI**: `docker logs ix-localai-localai-1`. It logs HTTP status, not causes;
  a refused call's reason is in the response body only.
- **Scripts**: `/root/louise-scheduled/*.log`, `/root/wikipedia-v8/build-night.log`,
  each campaign's `/root/*.log`.
- **The answer stream**: the web logs each Ask answer's end. `query_log` in each
  manifest records every search with its timings (`t_fts`, `t_dense`, `t_rerank`, …).

## 12. Measuring (summary of docs/notes/measuring.md)

- **Measure the deployed thing**: the deployed image or a mounted tree, the deployed env.
- **A harness that cuts records** uses the parse pool, so it runs as a FILE under
  `if __name__ == "__main__":` with `-e PYTHONPATH=/srv`, never from stdin. From
  stdin, the spawned children find no main file and the pool silently goes serial.
  Search-only harnesses may come on stdin.
- **Profiling**: `pyspy:local` from the host (`--pid=host --cap-add SYS_PTRACE`), as
  `py-spy dump` or `record --idle` (blocked threads) or `record --gil` (who holds the
  GIL). `docker top <c> -eo pid,args` needs the `pid` column.
- **Wikipedia**: normalise by chunks, and say what else ran (scheduling-log rules).

## 13. Common failures

| symptom | cause | what to do |
|---|---|---|
| a build does not exit after its stop (Python 3.12) | the pool lost its children (CPython gh-115634), exit joins forever | fixed in c3dcb4c; `wikipedia-build.sh stop` SIGKILLs after 15 min, nothing lost |
| a build at N children runs at one core | the stream waited on its oldest part (fixed 21da0e3) | profile with `--gil`; check `build-children.log` |
| rerank answers 500 at night; searches keep the fused order | the card shared with corpus embedding | 50030dc: priority + one retry; the reason now carries LocalAI's message |
| a model vanishes mid-work, or VRAM full | WatchDog eviction above 0.85; xe overcommit spills to host RAM | check `/system` and `/sys/kernel/debug/dri/0/vram0_mm`; unload idle models |
| after a reboot: governor or encoder fails, wrong card | DRM card numbers swapped | `ls -l /sys/class/drm/card*/device`; rebind by PCI address |
| `hold vectors show` reads covered far below what is held | stale `documents.chunks` (fixed by a targeted re-index), or the possession cache race (fixed f386934/5863f81: now no figure) | re-run once; `tools/measure/chunks_drift.py`, `coverage_explain.py` |
| replication FAILED or huge | a clone or a big new dataset in ssd4/ssd5 | abort the job, remove the clone (the operator's yes), confirm FINISHED |
| a timed job never ran | the client machine restarted (session timers die with it) | schedule node-side: cron, or a sleeper with `in-window.sh` |
| mounts land at `/mnt/mnt/…` | TrueNAS altroot on a created dataset | set the mountpoint explicitly |
| `pkill -f <pattern>` killed your own ssh session | the pattern matched the ssh command line | `docker stop/kill` by name, or a script file |

## 14. Open node-side items (2026-10-25)

Kept in `docs/notes/STATUS.md`; the list at the time of writing:

1. **wikidata, Monday 2026-10-26**: re-create (parser 2, sliced by family, map
   sha256:78399d60… approved), then discovery → text index → records build →
   snapshot → hold check. The pack is upgraded to v3 already.
2. **gutenberg**: the targeted re-index of the records with stale `documents.chunks`
   (`chunks_drift --all --ids-out` → `dev reindex --ids`). Then verify that the 127
   parts read fully covered.
3. **The rerank measurement (AFTER)**: the night of 2026-10-25 under 50030dc/888a9be.
   Read the per-call 500 rate, the bodies, per-query misses, the clock and the
   corpus slow-down. The heading-weight verdict (k = 2?) waits for those 117 queries.
4. **The next deploy**: 68dcde3 (boost removed) + 3fe8cb0 (05 §5.2 option A) +
   c14af77 (`--filter keys=`), and the parse-pool fixes (c3dcb4c, 7adde8a) for the
   worker. Afterwards, the TREC `none` arm must reproduce NDCG 0.3143.
5. **wikipedia-v8 build**: ~47 M records left on 2026-10-25 15:30, ~570 rec/s on 12
   cores. Then snapshot, hold check, the vector proof (wikipedia-qids.md §3, step 7).
6. **The first weekday stop** (Mon 06:00): time stop → exit and log it.
7. **Secrets**: move `ADMIN_TOKEN` from an env value to a file path (invariant 8);
   record where the node key's offline copy is.
8. **The dateless-record fix**: gutenberg RDF re-fetch, pubmed MedlineDate (counts in
   STATUS, 2026-10-25).
9. **Scratch on tank to delete when done**: `/mnt/tank/scratch-fts/k1,k2,k3` (30 GB
   each), `/mnt/tank/scratch-ab`, `/mnt/tank/scratch-trecpm`. Also the 99 GB
   version-1 wikidata pack beside the dump, once Monday's records build ends (the
   operator's yes, conditional).
