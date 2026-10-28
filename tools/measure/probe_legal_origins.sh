#!/usr/bin/env bash
# The legal template's step 1 for France, Germany and the EU
# (docs/notes/legal-fr-de-eu.md §7): probe each origin with curl BEFORE a
# fetch path is designed. Run from the node — the architecture session's
# network reaches none of these hosts — and paste the output raw.
#
#     bash tools/measure/probe_legal_origins.sh 2>&1 | tee /root/legal-probe-$(date +%Y%m%d-%H%M).log
#
# What it settles, per origin:
#   - DILA: the listing and its sizes, Range on the 1.1 GB baseline (1 KB
#     read), one daily LEGI increment and one JORF increment opened (a few MB);
#   - NeuRIS: the list, one expression's manifestations, the changelog, and
#     whether a bulk zip is served;
#   - Cellar: content negotiation for an act and a consolidated version
#     (XHTML, Formex), and one SPARQL count window.
# Nothing here is a baseline download: the whole run moves under ~20 MB.
# Only curl and python3 are used (python3 for JSON and a tar listing).
set -u
UA="louise-probe/1 (operator's node; legal template step 1)"
C=(curl -sS -A "$UA" --max-time 120)
section() { printf '\n===== %s =====\n' "$*"; }
head_of() { "${C[@]}" -o /dev/null -D - "$@" | tr -d '\r' | grep -iE '^(HTTP/|content-type|content-length|accept-ranges|location|last-modified|etag|x-ratelimit|retry-after)'; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------- France --
DILA=https://echanges.dila.gouv.fr/OPENDATA
for corpus in LEGI JORF; do
    section "DILA $corpus: listing (HEAD, then the files and sizes)"
    head_of "$DILA/$corpus/"
    "${C[@]}" "$DILA/$corpus/" > "$TMP/$corpus.html"
    python3 - "$TMP/$corpus.html" <<'EOF'
import re, sys
html = open(sys.argv[1], encoding="utf-8", errors="replace").read()
names = re.findall(r'href="([^"?/][^"]*?\.(?:tar\.gz|dat))"', html)
print(f"{len(names)} files listed; first 3, last 5:")
for n in names[:3] + ["…"] + names[-5:]:
    print("  ", n)
EOF
done

section "DILA LEGI: Range on the baseline (1 KB)"
BASE=$(python3 - "$TMP/LEGI.html" <<'EOF'
import re, sys
html = open(sys.argv[1], encoding="utf-8", errors="replace").read()
got = re.findall(r'href="(Freemium_legi_global_[^"]+\.tar\.gz)"', html)
print(got[-1] if got else "")
EOF
)
echo "baseline: ${BASE:-NONE FOUND}"
if [ -n "$BASE" ]; then
    head_of "$DILA/LEGI/$BASE"
    "${C[@]}" -r 0-1023 -o /dev/null -w 'range 0-1023: HTTP %{http_code}, %{size_download} bytes\n' "$DILA/LEGI/$BASE"
fi

for corpus in LEGI JORF; do
    section "DILA $corpus: the newest daily increment, opened"
    INC=$(python3 - "$TMP/$corpus.html" "$corpus" <<'EOF'
import re, sys
html = open(sys.argv[1], encoding="utf-8", errors="replace").read()
got = sorted(re.findall(rf'href="({sys.argv[2]}_\d{{8}}-\d{{6}}\.tar\.gz)"', html))
print(got[-1] if got else "")
EOF
)
    echo "increment: ${INC:-NONE FOUND}"
    [ -z "$INC" ] && continue
    "${C[@]}" -o "$TMP/inc.tar.gz" -w 'HTTP %{http_code}, %{size_download} bytes, %{time_total} s\n' "$DILA/$corpus/$INC"
    python3 - "$TMP/inc.tar.gz" <<'EOF'
import collections, sys, tarfile
with tarfile.open(sys.argv[1]) as t:
    names = [m.name for m in t.getmembers() if m.isfile()]
    kinds = collections.Counter(n.split("/")[-1][:8] for n in names)
    print(f"{len(names)} files; by name prefix: {dict(kinds.most_common(8))}")
    for n in names[:5]:
        print("  ", n)
    first = next((m for m in t.getmembers() if m.isfile() and m.name.endswith(".xml")), None)
    if first:
        print(f"--- {first.name}, first 1500 bytes:")
        print(t.extractfile(first).read(1500).decode("utf-8", "replace"))
EOF
done

# --------------------------------------------------------------- Germany --
for NEURIS in https://testphase.rechtsinformationen.bund.de https://rechtsinformationen.bund.de; do
    section "NeuRIS at $NEURIS: the list (size=2)"
    head_of "$NEURIS/v1/legislation?size=2"
    "${C[@]}" "$NEURIS/v1/legislation?size=2" -H 'Accept: application/json' > "$TMP/neuris.json"
    python3 - "$TMP/neuris.json" <<'EOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("not JSON:", e); print(open(sys.argv[1], errors="replace").read(600)); sys.exit()
print("top-level keys:", list(d)[:12])
print("total:", d.get("totalItems", d.get("total", "?")))
items = d.get("member") or d.get("items") or []
for it in items[:2]:
    print(json.dumps(it, ensure_ascii=False)[:1500])
EOF
    section "NeuRIS at $NEURIS: changelog, bulk"
    for path in "/v1/legislation/changelog" "/v1/legislation/bulk-zip-links" "/v1/bulk"; do
        printf -- '-- %s\n' "$path"
        head_of "$NEURIS$path"
        "${C[@]}" "$NEURIS$path" -H 'Accept: application/json' | head -c 800; echo
    done
done

# ------------------------------------------------------------------- EU --
CELLAR=https://publications.europa.eu/resource/celex
for celex in 32016R0679 02016R0679-20160504; do
    for accept in 'application/xhtml+xml' 'application/xml;type=fmx4' 'application/xml;notice=branch'; do
        section "Cellar $celex, Accept: $accept"
        "${C[@]}" -o /dev/null -D - -H "Accept: $accept" -H 'Accept-Language: eng' "$CELLAR/$celex" \
            | tr -d '\r' | grep -iE '^(HTTP/|content-type|location)'
    done
done

section "Cellar SPARQL: one counted window (consolidated versions of 2016 regulations)"
Q='PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT (COUNT(DISTINCT ?w) AS ?n) WHERE { ?w cdm:resource_legal_id_celex ?c . FILTER(STRSTARTS(STR(?c), "02016R")) }'
"${C[@]}" -w '\nHTTP %{http_code}, %{time_total} s\n' https://publications.europa.eu/webapi/rdf/sparql \
    -H 'Accept: application/sparql-results+json' --data-urlencode "query=$Q"

section "done"
