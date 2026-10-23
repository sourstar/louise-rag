# The München agent in a container (this PC's Docker Desktop)

Operator, 2026-10-23: the agent that drives the node runs in a container on
this PC's Docker Desktop, so it does not depend on a VS Code window or a login
session. After Perugia's (D:\louise-agent), adapted to this PC.

## Start

    cd deploy/agent
    docker build -t louise-agent:local .     # compose 5's `--build` (bake) asks for --allow=fs.read=Dockerfile
    docker compose up -d                     # or `docker start louise-agent` once it exists
    docker exec -it louise-agent claude      # first time only: /login, trust the folder, exit

The main process waits until a login exists, then runs `claude remote-control
--name München`: a persistent Remote Control server in the repo, reachable from
claude.ai/code or the app; it starts within 30 s of the first /login.

Prepared 2026-10-23 (image built, memory seeded, git/docker/ssh checked) and left
STOPPED: the operator switches to it himself.

`restart: unless-stopped` brings it back after a Docker Desktop restart.

## What it holds

- **The repo at the same path the daemon resolves**: `C:\Users\alana\OneDrive\
  Documenti\Projects\louise-rag` → `/run/desktop/mnt/host/c/Users/alana/OneDrive/
  Documenti/Projects/louise-rag`. A path the agent hands to `docker run -v`
  (the gate: `-v "$PWD:/srv"`) therefore resolves on the host unchanged.
  Scratch work goes under `C:\Users\alana\louise-agent-scratch`, mounted alike.
- **The docker socket** — control of every container on this PC.
- **The node's ssh key**, copied from `C:\Users\alana\.ssh` read-only at start
  with the permissions ssh requires. Reach the node with
  `ssh -i /root/.ssh/truenas_root -o BatchMode=yes root@192.168.178.10`.
- **A persistent home** (`agent-home` volume): `/login`, settings and memory
  survive a rebuild. Memory lives under the slug of the container's working
  directory: `~/.claude/projects/-run-desktop-mnt-host-c-Users-alana-OneDrive-
  Documenti-Projects-louise-rag/memory`.

## What it cannot do

- Nothing on the NAS runs locally: TrueNAS middleware (`midclt`), ZFS, the
  host's cron and scripts are reached over ssh, as before.
- This PC's own tools outside Docker (PowerShell, Windows event log) are out
  of reach; a restart's cause on this PC is read from the host.
- The usage rules still hold: the node's quiet hours, nice/ionice for
  unattended work, one intensive test an hour by day.
