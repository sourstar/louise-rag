# fp_runs.py — for each collection named: the run answering each side now, and the fingerprint its kept
# statement states ("none" where no statement is kept for that run). Read-only; fed on stdin to the worker:
#   docker exec -i ix-louise-rag-worker-1 python - gutenberg wikipedia < fp_runs.py
# One line per side: `<collection> <role> <run> <fingerprint>`, the role's statement path as app/hold/runs.py names it.
import json
import sys
from pathlib import Path

from app import collections as c, run_check
from app.hold import collection as hold_collection, runs

ROOT = Path("/data/collections")

for name in sys.argv[1:]:
    recipe = c.recipe_for(c.by_name(name))
    _, codex = hold_collection.resolve(ROOT, name)
    # the roles the gate states, as `hold embedder check` takes them (app/louise.py _embedder_check_runs)
    for role in ["document"] + (["query"] if codex.space.query.encoder is not None else []):
        run = run_check.answering_run(recipe, query=role == "query")
        p = runs.path_for(ROOT, name, run, role)
        fp = "none"
        if p.is_file():
            body = json.loads(p.read_text())["body"]
            body = json.loads(body) if isinstance(body, str) else body
            fp = body.get("fingerprint") or "none"
        print(name, role, run, fp, flush=True)
