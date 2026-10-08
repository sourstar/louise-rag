"""Which producer keys the legacy bundle inbox refuses — read off the ONE
trust list, the peer-wide `state/trust.json` (app/hold/trust_file.py; 09
§9.8), which `louise hold key trust <key> [--deny|--remove|--restore]`
edits.

WHAT THE SIGNATURE DOES NOT PROVE. The producer's public key sits inside the
header the producer signs, so a valid signature proves only that whoever holds
that private key produced exactly these bytes. It is integrity, not
authenticity: keys are free and instant to generate, so a signature alone
establishes nothing about whether the sender should be believed.

NO TRUST ON FIRST USE (03 §3.5; 08 §8.1). This inbox used to pin the first
producer key a collection saw, in the manifest's `providers` table, and refuse
every later one until an operator intervened — a list no command could change
once `bundle trust` was retired (2026-10-08), and one that disagreed with the
peer-wide list. Admission is the checks (app/bundles/checks.py and the ladder)
and nothing else; the trust list's DENY side and its burns are a mute — a key
denied or burned is one this peer accepts no traffic from, bundles included —
and its ALLOW side is read only when the caller asks for `strict`, which
admits nothing while the list is empty. `providers` keeps the bundle counts
(app/bundles/ledger.py); its `trusted`/`quarantined` columns are no longer
read or written.

A BURN IS PEER-WIDE. A verify-on-use failure burns the key in the trust file
(`quarantine_key`), so it is refused by every collection's inbox, not only by
the one where the forgery surfaced — and only `hold key trust <key> --restore`
lifts it.
"""
from pathlib import Path

from ..hold import trust_file
from .format import BundleError


class BundleUntrusted(BundleError):
    """This producer key may not write to the index. Terminal: retrying the
    same bundle fails identically until an operator acts."""


def get_provider(con, pubkey: str) -> dict | None:
    """The key's bundle counts in this collection (the `providers` row)."""
    row = con.execute("SELECT * FROM providers WHERE pubkey=?", (pubkey,)).fetchone()
    return dict(row) if row else None


def quarantine_key(con, pubkey: str, *, root: Path | None = None) -> dict[str, int]:
    """Burn the key in the peer-wide trust file (03 §3.5) and report which
    uids this producer had written HERE, so the caller can re-embed exactly
    those (architecture-v2.md §6's 'quarantine the key'). A key already
    burned — the worker's proof of forgery burns first, with its
    attestation — is left as it is."""
    from . import ledger
    trust_file.burn(trust_file.key_text(pubkey) or pubkey, None, root=root)
    return ledger.uids_written_by(con, pubkey)


def check_admission(pubkey: str, *, strict: bool = False,
                    root: Path | None = None) -> str:
    """Decide whether `pubkey` may import. Returns the reason it was allowed:
    `"listed"` (on the allow side) or `"admitted"` (any other unmuted key).

    Raises BundleUntrusted for a key denied or burned on the trust list, and,
    under `strict`, for one not on its allow side. The mute is checked FIRST
    so no later branch can bypass it.
    """
    why = trust_file.refusal(pubkey, root)
    if why is not None:
        raise BundleUntrusted(why)
    key = trust_file.key_text(pubkey)
    listed = key in trust_file.as_filter(root)["allow"]
    if strict and not listed:
        raise BundleUntrusted(
            f"strict: producer {key} is not on this peer's allow list "
            f"({trust_file.path(root)}). Add it with `louise hold key trust {key}`")
    return "listed" if listed else "admitted"
