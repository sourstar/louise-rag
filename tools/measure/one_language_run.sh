#!/bin/sh
# Starts the throwaway search container of tools/measure/one_language_search.py
# on the node: the live web container's image, network namespace and env
# (tokens left out), with EVERY mount read-only — /data as the web sees it,
# the SHARDs, the config, and /root/onelang (the script and the queries).
# Results go to stdout; nothing is written on the node.
# Usage: ssh node 'sh /root/onelang/one_language_run.sh' < /dev/null > results.jsonl
set -e
IMG=$(docker inspect ix-louise-rag-web-1 --format '{{.Image}}')
docker run --rm --name onelang-search --network container:ix-louise-rag-web-1 \
  --env-file /dev/stdin \
  -v /mnt/ssd4/apps/louise-rag/data:/data:ro \
  -v /mnt/ssd4/apps/louise-rag/config:/config:ro \
  -v /mnt/tank/apps/louise-shard/gutenberg:/data/collections/gutenberg/shard:ro \
  -v /mnt/tank/apps/louise-shard/pubmed:/data/collections/pubmed/shard:ro \
  -v /mnt/tank/apps/louise-shard/wikipedia:/data/collections/wikipedia/shard:ro \
  -v /root/onelang:/onelang:ro \
  --tmpfs /fts:rw,size=512m -e PYTHONPATH=/srv "$IMG" nice -n 10 python /onelang/${SCRIPT:-one_language_search.py} /onelang/queries.jsonl \
  <<ENV
$(docker inspect ix-louise-rag-web-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -v -E '^(ADMIN_TOKEN|DEPOSIT_TOKEN_FILE)=' | grep .)
ENV
