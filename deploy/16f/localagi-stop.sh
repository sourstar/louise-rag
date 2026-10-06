#!/usr/bin/env bash
# localagi-stop.sh — step 16f's stop, run against the node (migration-plan 16f):
# one LocalAGI agent with Louise as its knowledge base — memory stored, found,
# compacted; a file uploaded through LocalAGI's knowledge-base routes (the ones
# its page uses); one URL source after the web-sources acceptance. Everything
# goes through LocalAGI (:30050), which proxies its collection routes to
# Louise with the deposit token; nothing here talks to Louise directly except
# the final `hold collection show`. The agent `louise-stop` is LEFT IN PLACE
# for the operator to look at in the UI.
#   ssh … root@192.168.178.10 bash -s < deploy/16f/localagi-stop.sh
set -uo pipefail
AGI=http://127.0.0.1:30050
A=louise-stop
B=/tmp/stop-body
say() { printf '\n=== %s\n' "$*"; }
j() { curl -sS -o "$B" -w '%{http_code}' --max-time "${T:-30}" -H 'Content-Type: application/json' "$@"; }
body() { head -c "${1:-300}" "$B"; echo; }
ask() {  # a synchronous turn through the OpenAI-style responses route; the agent is the model
  T=420 j -X POST "$AGI/v1/responses" -d "{\"model\":\"$A\",\"input\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1")}" >/dev/null
  python3 - "$B" <<'P'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(open(sys.argv[1]).read()[:400]); sys.exit()
texts = []
def walk(x):
    if isinstance(x, dict):
        if x.get("type") == "output_text" and x.get("text"): texts.append(x["text"])
        for v in x.values(): walk(v)
    elif isinstance(x, list):
        for v in x: walk(v)
walk(d)
print((" ".join(texts) or json.dumps(d))[:500])
P
}
entries() { T=60 j "$AGI/api/collections/$A/entries" >/dev/null; python3 -c "import json; d=json.load(open('$B')); print(d.get('data', d))"; }

say "0. before: agents and Louise's collections, as LocalAGI's page sees them"
curl -s "$AGI/api/agents" | head -c 200; echo
T=30 j "$AGI/api/collections" >/dev/null; body 300

say "1. the agent: knowledge base on, long-term memory on, daily compaction with a summary"
code=$(j -X POST "$AGI/api/agent/create" -d "{\"name\":\"$A\",\"model\":\"qwen3.8-2b-q4\",\"system_prompt\":\"Sei l'assistente di prova di Louise. Rispondi in italiano, in una o due frasi, usando ciò che ricordi.\",\"enable_kb\":true,\"kb_results\":3,\"long_term_memory\":true,\"summary_long_term_memory\":false,\"enable_kb_compaction\":true,\"kb_compaction_interval\":\"daily\",\"kb_compaction_summarize\":true}")
echo "$code"; body 200
sleep 3

say "2. memory STORED: one turn with a fact; LocalAGI writes the memory to Louise after the reply"
echo "reply: $(ask "Ricorda questo: il mio caffè preferito è l'espresso del bar Centrale, e lo prendo alle otto.")"
for i in $(seq 1 24); do n=$(entries | python3 -c "import sys,ast; print(ast.literal_eval(sys.stdin.read()).get('count',0))" 2>/dev/null || echo 0); [ "${n:-0}" -ge 1 ] && break; sleep 5; done
echo "entries after the first turn: $(entries)"

say "3. memory FOUND: a second turn that needs it; the knowledge-base lookup runs against Louise"
echo "reply: $(ask "Qual è il mio caffè preferito e a che ora lo prendo?")"
echo "--- LocalAGI's lookup lines:"
docker logs --since 10m ix-localagi-localagi-1 2>&1 | grep -E "Knowledge Base Lookup" | grep "$A" | tail -4 | cut -c1-200

say "4. a FILE through LocalAGI's knowledge-base route (what its page's upload does)"
printf 'Appunti della riunione del 6 ottobre: il deposito pilota si chiama deposit-granite e usa granite-embedding-107m-multilingual.\n' > /tmp/stop-appunti.txt
code=$(curl -sS -o "$B" -w '%{http_code}' --max-time 120 -X POST -F "file=@/tmp/stop-appunti.txt" "$AGI/api/collections/$A/upload"); echo "$code"; body 200
echo "entries: $(entries)"
code=$(T=60 j -X POST "$AGI/api/collections/$A/search" -d '{"query":"come si chiama il deposito pilota","max_results":3}'); echo "search $code"; body 400

say "5. one URL SOURCE, after the node's acceptance of the web-sources disclaimer (04 §4.4)"
docker exec ix-louise-rag-worker-1 python -m app.louise hold collection ack --web-sources --yes 2>&1 | tail -3
code=$(T=60 j -X POST "$AGI/api/collections/$A/sources" -d '{"url":"https://example.com/","update_interval":1440}'); echo "add source: $code"; body 300
for i in $(seq 1 24); do T=60 j "$AGI/api/collections/$A/sources" >/dev/null; grep -q -i -E "last_fetch|fetched|\"20" "$B" && break; sleep 5; done
echo "sources: $(head -c 400 "$B")"; echo
echo "entries (the source's entry under LocalRecall's key): $(entries)"

say "6. memory COMPACTED: compaction runs when the agent starts — pause, start, then look"
T=60 j -X PUT "$AGI/api/agent/$A/pause" >/dev/null; sleep 2
T=60 j -X PUT "$AGI/api/agent/$A/start" >/dev/null
before=$(entries)
for i in $(seq 1 36); do sleep 5; now=$(entries); [ "$now" != "$before" ] && break; done
echo "entries before: $before"
echo "entries after:  $now"
echo "--- compaction lines:"
docker logs --since 10m ix-localagi-localagi-1 2>&1 | grep -i -E "compact" | tail -6 | cut -c1-200

say "7. Louise's side"
docker exec ix-louise-rag-worker-1 python -m app.louise hold collection show "$A" --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('state:', d['state'], '| origin_check:', d.get('origin_check'), '| space:', d['space_id'][:23])"
docker logs --since 15m ix-louise-rag-web-1 2>&1 | grep -E "/api/collections/$A" | sed -E 's/^INFO: +//' | cut -c1-110 | sort | uniq -c | sort -rn | head -12
docker logs --since 15m ix-louise-rag-worker-1 2>&1 | grep -i -E "$A|sources" | tail -6 | cut -c1-160
echo
echo "The agent $A stays for the operator (LocalAGI's UI, :30050)."
