#!/bin/sh
# Runs tools/measure/one_language_rep_search.py on the node with nothing
# copied there: the script, its queries embedded (one_language_rep_build.py),
# goes over ssh's stdin to `python -` in a throwaway container — the live
# web container's image, network namespace and env (tokens left out), every
# mount read-only. Results go to stdout.
# Usage (Git Bash, repo root): sh tools/measure/one_language_rep_run.sh BUILT.py > out.jsonl
K=/c/Users/alana/.ssh/truenas_root
N=root@192.168.178.10
REMOTE=$(cat <<'RSH'
IMG=$(docker inspect ix-louise-rag-web-1 --format '{{.Image}}')
docker run --rm -i --name onelang-rep --network container:ix-louise-rag-web-1 \
  --env-file <(docker inspect ix-louise-rag-web-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -v -E '^(ADMIN_TOKEN|DEPOSIT_TOKEN_FILE)=' | grep .) \
  -v /mnt/ssd4/apps/louise-rag/data:/data:ro \
  -v /mnt/ssd4/apps/louise-rag/config:/config:ro \
  -v /mnt/tank/apps/louise-shard/gutenberg:/data/collections/gutenberg/shard:ro \
  -v /mnt/tank/apps/louise-shard/pubmed:/data/collections/pubmed/shard:ro \
  -v /mnt/tank/apps/louise-shard/wikipedia:/data/collections/wikipedia/shard:ro \
  --tmpfs /fts:rw,size=512m -e PYTHONPATH=/srv "$IMG" nice -n 10 python -
RSH
)
B64=$(printf '%s\n' "$REMOTE" | base64 -w0)
ssh -i $K -o BatchMode=yes -o ServerAliveInterval=60 $N "bash -c \"\$(echo $B64 | base64 -d)\"" < "$1"
