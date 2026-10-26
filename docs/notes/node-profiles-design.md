# Node profiles: quiet, day and night, from one file

*Proposal, 2026-10-26, for the operator's review. Nothing is built. The
operator, the same day: "Design a configuration for the quiet/performance
cpu/gpu processing and also to switch collections to full embedding. We can
start with a configuration file." Example file:
[node-profiles.example.toml](node-profiles.example.toml).*

## What it replaces

Today the node's schedule is six root crons calling scripts that edit state
from outside:
- `set-embed.sh` rewrites a collection file's `embed` line twice a day;
- `wikipedia-build.sh` starts and stops the build container and sets its cores;
- `gpu-cap.sh` writes the card's `max_freq`;
- a hold file pauses the build by hand.

Each script carries its own copy of the times, so changing the schedule means
editing several crons and scripts that must agree.

## The proposal

**One declaration.** A `[schedule]` and `[profiles]` section in
`platform.toml`, as in the example file. Each profile says three things:
- `cpu_cores`: how many cores background CPU work may use;
- `gpu`: a GPU level (`full`, `quiet`);
- `sustained_embedding`: whether collections set to embed everything may do so now.

The schedule picks the active profile by local time, in the grammar
`[bandwidth] schedule` already uses, with the same parser.

**Collections keep saying WHAT.** A collection's file says `embed = "all"`
(or `on-demand`, `off`, `p2p`), set with `louise hold config apply <c> --set
embed=all`. That command is built and is the one way to switch a collection
to full embedding. The profile says WHEN: while `sustained_embedding` is
false, an `all` collection behaves as `on-demand`, embedding only what
queries touch. An optional `embed_profiles = ["night"]` in a collection's
file narrows it further. No file is rewritten on a timer, and nothing is
written into the state database (invariant 10). The effective mode is
computed by the same function the worker uses, and `hold config show` gives
it with its reason ("on-demand: quiet until 08:00").

**Precedence.** In order, each wins over the next:
1. the node's mode (`node pause`, acquire-only, seed-only), the manual override that survives a restart;
2. the active profile;
3. the collection's own `embed`.

Queries are never limited by a profile.

## Who applies what

| setting | applied by | how |
|---|---|---|
| sustained embedding | the worker | reads the active profile each round; a change takes effect at the next round |
| `cpu_cores` | the records build and the worker's parse pool | the pool's size is `min(cpu_cores, the cgroup quota)` (`config.available_cpus`); a change resizes it at the next page; `0` finishes the current part, commits and waits, then resumes when a profile allows. No container is stopped or started |
| `gpu` level | the GPU governor app (already writes the card's `min_freq`) | writes hwmon `power1_max` (the standard Linux power limit) where writable, else `max_freq`; restores on leaving the level; keeps its query hold at or below the cap |
| fans | the operator's `fancontrol.py`, optionally | reads `louise node profile show --json` to cap case fans in `quiet` |

The container's `cpu.max` stays as a fixed hard ceiling (the night's 12
cores): the kernel's limit, while the app limits itself below it.

## What the operator sees

- `louise node profile show`: the active profile, until when, the next one,
  and what each applier did (pool size, GPU limit, which collections are
  waiting for their profile).
- The Library's card says it in plain words: "embedding resumes at 08:00 (quiet hours)".
- `node show` gains one line: the active profile.

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

## Decisions for the operator

**1. Where the schedule lives.**

| option | pros | cons |
|---|---|---|
| a section of `platform.toml` (recommended) | one node-level file with `[bandwidth]`'s schedule beside it | `platform.toml` grows |
| a separate `profiles.toml` | can be edited on its own | a second node-level file to keep in step |

**2. How CPU is limited.**

| option | pros | cons |
|---|---|---|
| the app sizes its pools, `cpu.max` as a fixed ceiling (recommended) | no container restarts; a change applies at the next page | relies on the app honouring it, with the kernel's ceiling as the backstop |
| the host resets `cpu.max` per period (today) | the kernel enforces each period | stops and starts containers; schedule copies in crons |

**3. Embedding during quiet periods.**

| option | pros | cons |
|---|---|---|
| on-demand: only what queries touch (recommended) | search works fully at any hour | a query can briefly load the GPU in a quiet period |
| off | silence guaranteed | a new query's documents wait until the next day period |

If the operator says nothing, the recommended options are built after the
review.
