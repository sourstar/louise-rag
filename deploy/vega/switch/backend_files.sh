# backend_files.sh — sourced by the rollouts that write INTO LocalAI's backends/
# (rollout-110-20261030.sh@c572c3b; the louise-python-20261030 build by hand).
#
# THE TRAP (2026-10-30): backends/ is an nfsv4 dataset (aclmode passthrough)
# whose directories carry an inheritable `user:apps:…x…:fd` entry. A file
# CREATED there — by cp -a, install, tar, compileall — inherits it, and its
# mode then reads owner-execute (644 → 744, 664 → 764); chmod alone does not
# clear it. A tree digest counts each file's exec bit (vega_wrapper.
# tree_manifest; backend-tree-drift), so a copy that passes every functional
# check comes out a different engine. Every write here therefore ends with
# the written paths stripped to a trivial ACL and given back the mode they
# had (or that the source has), and checked.
#
#   bf_record DIR PATH...        "mode<TAB>path" for each PATH under DIR (to a file)
#   bf_restore DIR MODES         strip each recorded path's ACL, chmod it back, check
#   bf_copy SRC DST              SRC's tree onto DST without ACLs or xattrs (tar)

bf_record() {
  local dir=$1; shift
  local p
  for p in "$@"; do
    printf '%s\t%s\n' "$(stat -c %a "$dir/$p")" "$p"
  done
}

bf_restore() {
  local dir=$1 modes=$2 m p bad=0
  while IFS=$'\t' read -r m p; do
    [ -n "$p" ] || continue
    nfs4xdr_setfacl -b "$dir/$p"
    chmod "$m" "$dir/$p"
  done < "$modes"
  while IFS=$'\t' read -r m p; do
    [ -n "$p" ] || continue
    if [ "$(stat -c %a "$dir/$p")" != "$m" ]; then
      echo "MODE $dir/$p: $(stat -c %a "$dir/$p"), expected $m"; bad=1
    fi
    if ! nfs4xdr_getfacl "$dir/$p" | grep -q "acl-is-trivial"; then
      echo "ACL $dir/$p: not trivial"; bad=1
    fi
  done < "$modes"
  return $bad
}

bf_copy() {
  tar -C "$1" --no-acls --no-xattrs -cf - . | tar -C "$2" --no-acls --no-xattrs --no-same-permissions -xf -
}
