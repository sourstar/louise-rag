#!/bin/sh
# A Louise backend for LocalAI (migration step 15e-2; louise_backend.py): one
# copy of this file in each backend's directory under LocalAI's system
# backends path, beside its backend.py and its venv. LocalAI runs it as
# `run.sh --addr 127.0.0.1:<port>`; `exec` keeps the pid LocalAI lists, so the
# process RAGE reads is the backend's python, whose program lies in the
# image's backends path (app/rage/observer.py).
#
# Nothing is installed here: LocalAI's own run.sh creates a missing venv and
# downloads into it, which would be a backend no image holds (09 §9.12).
set -eu
# ABSOLUTE: LocalAI starts this as `./run.sh` from the backend's directory
# (seen in the smoke test, rage-over-localai §11), and the argument vector is
# the engine's `args` (09 §9.3) — a program named relative to a working
# directory the run does not state.
dir=$(cd "$(dirname "$0")" && pwd)
# lib/: libraries that must come before the base's. torch's XPU wheels carry a
# SYCL runtime newer than the base's oneAPI loader, and `import torch` dies on
# "undefined symbol: urDeviceWaitExp" behind it (deploy/rage/Dockerfile).
# LocalAI's own backends do the same with their lib/.
if [ -d "$dir/lib" ]; then
  export LD_LIBRARY_PATH="$dir/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec "$dir/venv/bin/python" "$dir/backend.py" "$@"
