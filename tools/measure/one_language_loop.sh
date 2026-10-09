#!/bin/sh
# Local driver (Git Bash): runs the pending queries on the node until none is
# left or five passes have run. Results append to out/onelang/results.jsonl;
# a query recorded with an error is pending again on the next pass.
cd "$(dirname "$0")/../.." || exit 1
O=tools/measure/out/onelang
K=/c/Users/alana/.ssh/truenas_root
N=root@192.168.178.10
for pass in 1 2 3 4 5; do
  MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W):/srv" -w /srv louise-rag:dev-merge \
    python -I tools/measure/one_language_pending.py ${TSV:-tools/measure/one_language_queries.tsv} $O/results.jsonl > $O/pending.jsonl
  n=$(wc -l < $O/pending.jsonl)
  echo "pass $pass: $n pending" >> $O/run.log
  [ "$n" -eq 0 ] && break
  scp -q -i $K tools/measure/one_language_search.py tools/measure/one_language_run.sh $N:/root/onelang/
  scp -q -i $K $O/pending.jsonl $N:/root/onelang/queries.jsonl
  ssh -i $K -o ServerAliveInterval=60 $N 'sh /root/onelang/one_language_run.sh' < /dev/null >> $O/results.jsonl 2>> $O/run.log
done
