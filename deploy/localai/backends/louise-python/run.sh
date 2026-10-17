#!/bin/sh
# louise-python for LocalAI (backend.py). LocalAI runs it as
# `run.sh --addr 127.0.0.1:<port>`; `exec` keeps the pid LocalAI lists.
#
# NO VENV: the packages are in the portable CPython's own prefix, so the
# backend runs wherever it is unpacked. A venv names its interpreter by an
# absolute path (pyvenv.cfg), and a gallery or GUI install chooses the
# directory. Nothing is installed or written here (09 §9.12): -B, and every
# module is compiled at build time.
set -eu
dir=$(cd "$(dirname "$0")" && pwd)
# python/lib: torch's SYCL runtime before the base image's older oneAPI
# loader, or `import torch` dies on "undefined symbol: urDeviceWaitExp"
export LD_LIBRARY_PATH="$dir/python/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# -E -s: no PYTHON* variable and no user site from the image it runs in
# (LocalAI's Intel image sets PYTHONPATH to oneAPI's advisor)
exec "$dir/python/bin/python3" -B -E -s "$dir/backend.py" "$@"
