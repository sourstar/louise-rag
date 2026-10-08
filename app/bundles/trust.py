"""Which producer keys may write vectors into this node's index.

WHAT THE SIGNATURE DOES NOT PROVE. The producer's public key sits inside the
header the producer signs, so a valid signature proves only that whoever holds
that private key produced exactly these bytes. It is integrity, not
authenticity: keys are free and instant to generate, so a signature alone
establishes nothing about whether the sender should be believed.

TRUST ON FIRST USE. The first producer key this collection sees is recorded and
accepted; a different key later is refused until an operator trusts it
explicitly. TOFU is defensible HERE — and only here — because admission does
not rest on it: every bundle passes app/bundles/checks.py's gates regardless of
who signed it, so a wrong-model or garbage bundle is rejected no matter how
trusted its key is. What trust governs is the residue those checks cannot
see — a handful of deliberately mismapped vectors inside an otherwise honest
bundle — and that residue is what verify-on-use exists to catch.

QUARANTINE ONLY EVER SUBTRACTS. Trust is granted in Tier-2 config or by an
explicit operator verb; quarantine is a Tier-3 runtime override that can
withdraw it but never grant it (CLAUDE.md invariant 10). A quarantined key
stays quarantined until an operator clears it by name.
"""
import time

from .format import BundleError


class BundleUntrusted(BundleError):
    """This producer key may not write to the index. Terminal: retrying the
    same bundle fails identically until an operator acts."""


def get_provider(con, pubkey: str) -> dict | None:
    row = con.execute("SELECT * FROM providers WHERE pubkey=?", (pubkey,)).fetchone()
    return dict(row) if row else None


def trusted_keys(con) -> list[str]:
    return [r["pubkey"] for r in con.execute(
        "SELECT pubkey FROM providers WHERE trusted=1 AND quarantined=0 "
        "ORDER BY first_seen")]


def trust_key(con, pubkey: str, note: str = "") -> None:
    """Explicitly trust a key. Clears any quarantine, so this is also the
    'un-quarantine' path — deliberately requiring the operator to name the key
    rather than offering a blanket reset."""
    now = int(time.time())
    con.execute(
        "INSERT INTO providers(pubkey,trusted,quarantined,quarantine_reason,note,"
        "first_seen,last_seen) VALUES(?,1,0,NULL,?,?,?) "
        "ON CONFLICT(pubkey) DO UPDATE SET trusted=1,quarantined=0,"
        "quarantine_reason=NULL,note=excluded.note,last_seen=excluded.last_seen",
        (pubkey, note, now, now))


def quarantine_key(con, pubkey: str, reason: str) -> dict[str, int]:
    """Withdraw trust and report which uids this producer had written, so the
    caller can delete exactly those vectors (architecture-v2.md §6's
    'quarantine the key')."""
    from . import ledger
    now = int(time.time())
    con.execute(
        "INSERT INTO providers(pubkey,trusted,quarantined,quarantine_reason,"
        "first_seen,last_seen) VALUES(?,0,1,?,?,?) "
        "ON CONFLICT(pubkey) DO UPDATE SET trusted=0,quarantined=1,"
        "quarantine_reason=excluded.quarantine_reason,last_seen=excluded.last_seen",
        (pubkey, reason[:500], now, now))
    return ledger.uids_written_by(con, pubkey)


def check_admission(con, pubkey: str, *, allow_first_use: bool = True) -> str:
    """Decide whether `pubkey` may import. Returns the reason it was allowed.

    Raises BundleUntrusted otherwise. Quarantine is checked FIRST so it cannot
    be bypassed by any later branch.
    """
    provider = get_provider(con, pubkey)
    if provider and provider["quarantined"]:
        raise BundleUntrusted(
            f"producer {pubkey[:16]}… is quarantined "
            f"({provider['quarantine_reason'] or 'no reason recorded'}). "
            f"Clear it deliberately in this collection's `providers` table if that "
            f"was wrong (`bundle trust` was retired on 2026-10-08; the peer-wide "
            f"list, `louise hold key trust`, governs what arrives by `share`).")
    if provider and provider["trusted"]:
        return "trusted"

    existing = trusted_keys(con)
    if existing:
        raise BundleUntrusted(
            f"producer {pubkey[:16]}… is not trusted by this node, which already "
            f"trusts {len(existing)} other key(s). To accept it, mark it trusted "
            f"in this collection's `providers` table (`bundle trust` was retired "
            f"on 2026-10-08).")
    if not allow_first_use:
        raise BundleUntrusted(
            f"producer {pubkey[:16]}… is not trusted and first-use trust is "
            f"disabled. To accept it, mark it trusted in this collection's "
            f"`providers` table (`bundle trust` was retired on 2026-10-08).")

    # Trust on first use: nothing is trusted yet, so this key becomes the
    # anchor. Recorded explicitly rather than implied, so the operator can see
    # in the System page exactly whose vectors they took on.
    trust_key(con, pubkey, note="trusted on first use")
    return "first-use"
