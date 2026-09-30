#!/bin/sh
# Build main.go inside a LocalAGI checkout and run it against a Louise node.
#   run.sh <localagi-checkout> <louise-url> <deposit-token-file>
# The token stays in its file (invariant 8): the check reads it from there, so it
# is never on a command line any process listing would show.
set -eu
src=$1; url=$2; tokfile=$3
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$src/cmd/louise_check" "$tmp"' EXIT
mkdir -p "$src/cmd/louise_check"
cp "$here/main.go" "$src/cmd/louise_check/main.go"
(cd "$src" && go build -o "$tmp/check" ./cmd/louise_check)
"$tmp/check" "$url" "$tokfile"
