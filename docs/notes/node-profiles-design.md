# Node profiles: quiet, day and night, from one file

*Decided 2026-10-27 (operator: "1 and 2 your recommendations, 3 full
local/p2p, the whole point of quiet gpu is to use it"); being built. The
resolver is `app/node_profile.py`, `node show` gives the profile in force.
The operator, 2026-10-26: "Design a configuration for the quiet/performance
cpu/gpu processing and also to switch collections to full embedding. We can
start with a configuration file." Example file:
[node-profiles.example.toml](node-profiles.example.toml).*

## What it replaces

Today the node's schedule is six root crons calling scripts that edit state
from outside:
- `set-embed.sh` rewrites a collection file's `embed` line twice a day;
- `wikipedia-build.sh` starts and stops the build container and sets its cores;
- `gpu-cap.sh@c572c3b` writes the card's `max_freq`;
- a hold file pauses the build by hand.

Each script carries its own copy of the times, so changing the schedule means
editing several crons and scripts that must agree.

## The proposal

**One declaration.** A `[schedule]` and `[profiles]` section in
`platform.toml`, as in the example file. Each profile says two things:
`cpu_cores`, how many cores background CPU work may use (0 = paused at the
end of its unit), and `gpu`, a level (`full`, `quiet`). The schedule picks
the active profile by local time, in the grammar `[bandwidth] schedule`
already uses (the same parser, `parse_named`).

**Collections say WHAT, in every profile.** A collection's file says
`embed = "all"` (or `p2p`, `on-demand`, `off`), set with `louise hold
config apply <c> --set embed=all`. That is the one way to switch a
collection to full embedding, and it holds in every profile: a quiet GPU is
a power-limited one, still used (decision 3). No file is rewritten on a
timer; `set-embed.sh` retires.

**Precedence.** The node's mode (`node pause`, acquire-only, seed-only),
the manual override that survives a restart, wins over the profile. Queries
are never limited by a profile.

## Who applies what

| setting | applied by | how |
|---|---|---|
| `cpu_cores` | the records build and the worker's parse pool, and wikidata's pack pass (`app/cpu_limit.py`, built 2026-10-27) | the pool keeps its size (`config.available_cpus`); the parts IN FLIGHT are kept at `min(cpu_cores, the pool's window)`, re-read at each page (build) or batch (worker), cached 30 s or until the profile's own end. `0` pauses at that boundary: the pages in flight are finished and handed over, the parts they complete committed by the build's own per-part commit, the open part's records kept in memory; the worker's rdf, fts and archive passes stand down (embedding, bundles and scans go on). It waits in a bounded sleep that honours SIGTERM (exit 130, nothing committed lost) and resumes when a profile allows cores. Each change is logged once per collection ("profile quiet: paused until 08:00"); an unreadable `platform.toml` is logged and the last good limit kept. No container is stopped or started |
| `gpu` level | the GPU governor app (`app/gpu_level.py`, built 2026-10-27; it already wrote the card's `min_freq`) | reconciles every tick, writing only a difference: hwmon `power1_max` (the standard Linux power limit; "N%" of `power1_rated_max`, "NW", "max" = as read at its start) where writable, else `max_freq` ("N%" of `rp0_freq`, "NMHz"); a level a killed governor left on the card is not taken for the card's own. The query hold is clamped to the cap, and a cap below a live hold waits for it to end. `fan_max_rpm` only through a writable `fanN_max`, else the status says `fan: firmware-controlled` (xe). The card's own limits restored on a clean stop; no `[schedule]`, nothing written; a file that does not load keeps the last level. Shown in `control.gpu_governor` `level`, `node show` `profile.gpu_applied` and the System tab. Retires `gpu-cap.sh@c572c3b` |
| fans | the operator's `fancontrol.py`, optionally | reads `louise node show` to cap case fans in `quiet` |

The container's `cpu.max` stays as a fixed hard ceiling (the night's 12
cores): the kernel's limit, while the app limits itself below it.

## What the operator sees

- `louise node show` (and `GET /api/node/show`): the profile in force, its
  cores and GPU level, until when, and the next one.

## Standards, and why not them

- **`tuned` profiles** (Red Hat; `powersave`, `throughput-performance`) and
  **power-profiles-daemon** do this host-wide for CPU governors and power.
  They don't know Louise's stages, and TrueNAS doesn't let the host install
  packages. Worth adopting if the node moves to a general-purpose host.
- **systemd timers / cron** stay for the one thing they are for: none is
  needed for this, since every applier reads the clock itself.
- The appliers are a **reconciliation loop**, Kubernetes' pattern: each one
  compares the desired state for this minute with what is, and acts only on
  a difference. A missed tick or a restart converges on the next.

## Costs and risks

- About 3–4 days of work: the parser extension (names in place of factors),
  the resolver, the worker's gate, the pool resize, the governor's levels,
  `node profile show`, tests, and 07/STATUS/runbook amendments.
- The governor gains write access to the card's power limit. It is already
  privileged on the same device.
- DST: wall-clock windows move with local time, as `[bandwidth]`'s do; that
  is intended.
- The six crons are retired only after a week where both agree.

## Decisions (operator, 2026-10-27)

1. The schedule lives in `platform.toml`.
2. The app sizes its own pools; the container's `cpu.max` stays a fixed ceiling.
3. Embedding runs in every profile, local or p2p as each collection says; the
   quiet GPU is power-limited, not idle.
4. *(2026-10-27 21:31, supersedes the quiet GPU level of decision 3.)* The
   operator: "Set the GPU to maximum speed." In the node's `platform.toml`,
   `[profiles.quiet] gpu = "full"` (it was `"quiet"`, 110 W). The governor
   applied 230 W at 21:31:20. The quiet profile still pauses background CPU
   work (`cpu_cores = 0`). The `[gpu.levels.quiet]` table stays declared and
   unused. Backup: `/root/platform.toml.bak-20261027-gpufull`.
5. *(2026-11-02, supersedes the CPU halves of the schedule.)* The operator:
   "Use 12 CPU cores even in the quiet hours, I installed a new CPU cooler
   that is much more silent." `cpu_cores = 12` in every profile — quiet
   (was 0) and day (was 8) as night. The GPU's levels are unchanged.
