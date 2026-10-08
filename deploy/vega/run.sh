#!/bin/sh
# VeGa's wrapper as a LocalAI backend (deploy/vega/vega_wrapper.py; docs/notes/vega.md):
# one copy in each `vega-<backend>` directory under LocalAI's backends path,
# beside vega_wrapper.py, vega.json and its venv. LocalAI runs it as
# `run.sh --addr 127.0.0.1:<port>`; `exec` keeps the pid LocalAI lists. The
# wrapper starts the real backend's own run.sh as its child.
#
# Nothing is installed here: a venv made at run time would be code no image
# holds (09 §9.12).
set -eu
dir=$(cd "$(dirname "$0")" && pwd)
exec "$dir/venv/bin/python" "$dir/vega_wrapper.py" "$@"
