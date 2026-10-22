"""The node's users: who may search (07 §7.4, amended 2026-10-22).

Operator, 2026-10-22: "a search-only gui (aka user gui). A user token giving
only access to the ask tab", with add, remove, role, expiry, reissue and
revoke. A user is a NAME the operator chose, a ROLE — `user` (Ask, the
reader, pictures, translation, Help, Connect, and the machine interfaces:
`/v1`, `/api/ask`, MCP) or `admin` (everything, as the admin token) — an
optional expiry, and a token.

Desired state, so a TOML file: `${CONFIG_DIR}/users.toml`, beside
platform.toml (invariant 10). It holds the SHA-256 of each token, never the
token (invariant 8): the token is printed once, by `add` and `reissue`, and
a lost one is reissued, never recovered. `louise user …` and the admin API
are its writers, under one lock; the web reads it again when it changes.

`last_seen` is not desired state: it is kept in `<LOUISE_ROOT>/state/
users-seen.json`, written at most once a minute per user.

Until the first ACTIVE user exists, Ask stays open on the LAN as it always
was; from then on it needs a token (`auth.viewer`).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

ROLES = ("user", "admin")
PREFIX = "lu_"
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SEEN_EVERY_S = 60.0


class UserError(ValueError):
    """A request the users file cannot honour; the message names why."""


@dataclass(frozen=True)
class User:
    name: str
    role: str
    token_sha256: str
    created: str                     # ISO 8601 UTC, seconds
    expires: str | None = None       # ISO 8601 UTC; None = never
    revoked: str | None = None       # when it was revoked; None = not

    def state(self, now: dt.datetime | None = None) -> str:
        if self.revoked:
            return "revoked"
        if self.expires and _parse_time(self.expires) <= (now or _now()):
            return "expired"
        return "active"

    def public(self, seen: dict[str, str] | None = None) -> dict:
        """What may be shown: everything but the token's hash."""
        return {"name": self.name, "role": self.role, "created": self.created, "expires": self.expires,
                "revoked": self.revoked, "state": self.state(),
                "last_seen": (seen or {}).get(self.name)}


def path() -> Path:
    return Path(os.getenv("CONFIG_DIR", ".")) / "users.toml"


def _seen_path() -> Path:
    from app import config
    return config.louise_root() / "state" / "users-seen.json"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def parse_expiry(text: str | None, *, now: dt.datetime | None = None) -> str | None:
    """`never` | `<N>d` | `YYYY-MM-DD` (valid through that day, UTC) |
    an ISO 8601 time → the stored expiry, or None for never."""
    t = (text or "never").strip().lower()
    now = now or _now()
    if t in ("never", "none", ""):
        return None
    m = re.fullmatch(r"(\d{1,5})d", t)
    if m:
        return _iso(now + dt.timedelta(days=int(m.group(1))))
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            day = dt.date.fromisoformat(t)
            return _iso(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), dt.timezone.utc))
        return _iso(_parse_time(text.strip()))
    except ValueError:
        raise UserError(f"expiry {text!r}: `never`, `90d`, a date (2027-03-31) or an ISO time") from None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


# ---- the file ---------------------------------------------------------------

def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)        # a TOML basic string is a JSON string


def _dump(users: list[User]) -> bytes:
    out = ["# Louise users (07 §7.4). Written by `louise user …` and the admin API;",
           "# a token is never stored, only its sha256. Edit with the CLI.", ""]
    for u in sorted(users, key=lambda u: u.name):
        out.append("[[user]]")
        out.append(f"name = {_q(u.name)}")
        out.append(f"role = {_q(u.role)}")
        out.append(f"token_sha256 = {_q(u.token_sha256)}")
        out.append(f"created = {_q(u.created)}")
        if u.expires:
            out.append(f"expires = {_q(u.expires)}")
        if u.revoked:
            out.append(f"revoked = {_q(u.revoked)}")
        out.append("")
    return "\n".join(out).encode("utf-8")


def _parse(raw: bytes) -> list[User]:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise UserError(f"{path()} does not read ({e}); nobody but the admin token gets in") from None
    out = []
    for row in data.get("user") or []:
        role = str(row.get("role") or "user")
        if role not in ROLES:
            raise UserError(f"{path()}: user {row.get('name')!r} has role {role!r}; roles: {', '.join(ROLES)}")
        out.append(User(name=str(row["name"]), role=role, token_sha256=str(row["token_sha256"]),
                        created=str(row.get("created") or ""), expires=row.get("expires") or None,
                        revoked=row.get("revoked") or None))
    return out


_cache: tuple[tuple, list[User]] | None = None
_cache_lock = threading.Lock()


def load() -> list[User]:
    """Every user in the file, read again only when the file changed."""
    global _cache
    p = path()
    try:
        st = p.stat()
    except FileNotFoundError:
        return []
    stamp = (str(p), st.st_ino, st.st_size, st.st_mtime_ns)
    with _cache_lock:
        if _cache is not None and _cache[0] == stamp:
            return _cache[1]
    users = _parse(p.read_bytes())
    with _cache_lock:
        _cache = (stamp, users)
    return users


@contextmanager
def _editing() -> Iterator[list[User]]:
    """Read-modify-write under an exclusive lock beside the file; written
    to a sibling and renamed whole (invariant 2)."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name(".users.toml.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            users = _parse(p.read_bytes()) if p.exists() else []
            yield users
            tmp = p.with_name(".users.toml.tmp")
            tmp.write_bytes(_dump(users))
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _index(users: list[User], name: str) -> int:
    for i, u in enumerate(users):
        if u.name == name:
            return i
    raise UserError(f"no user {name!r}")


def _role(role: str) -> str:
    if role not in ROLES:
        raise UserError(f"role {role!r}: one of {', '.join(ROLES)}")
    return role


# ---- the verbs (CLI and admin API alike) --------------------------------------

def add(name: str, *, role: str = "user", expires: str | None = None) -> tuple[User, str]:
    """A new user and its token — the only time the token exists in clear."""
    if not NAME.fullmatch(name or ""):
        raise UserError(f"name {name!r}: lowercase letters, digits, '.', '_' or '-', up to 64")
    token = _new_token()
    with _editing() as users:
        if any(u.name == name for u in users):
            raise UserError(f"user {name!r} exists; `louise user reissue {name}` gives a new token")
        user = User(name=name, role=_role(role), token_sha256=hash_token(token), created=_iso(_now()),
                    expires=parse_expiry(expires))
        users.append(user)
    return user, token


def set_role(name: str, role: str) -> User:
    with _editing() as users:
        i = _index(users, name)
        users[i] = replace(users[i], role=_role(role))
        return users[i]


def set_expiry(name: str, expires: str | None) -> User:
    with _editing() as users:
        i = _index(users, name)
        users[i] = replace(users[i], expires=parse_expiry(expires))
        return users[i]


def reissue(name: str) -> tuple[User, str]:
    """A new token; the old one stops working at once. A revoked user is
    active again; role and expiry are kept."""
    token = _new_token()
    with _editing() as users:
        i = _index(users, name)
        users[i] = replace(users[i], token_sha256=hash_token(token), revoked=None)
        return users[i], token


def revoke(name: str) -> User:
    with _editing() as users:
        i = _index(users, name)
        if not users[i].revoked:
            users[i] = replace(users[i], revoked=_iso(_now()))
        return users[i]


def remove(name: str) -> None:
    with _editing() as users:
        del users[_index(users, name)]


# ---- who is asking --------------------------------------------------------------

def any_active() -> bool:
    now = _now()
    return any(u.state(now) == "active" for u in load())


def resolve(token: str | None) -> User | None:
    """The ACTIVE user this token belongs to, or None."""
    if not token or not token.startswith(PREFIX):
        return None
    want = hash_token(token)
    now = _now()
    for u in load():
        if secrets.compare_digest(u.token_sha256, want):
            return u if u.state(now) == "active" else None
    return None


_seen: dict[str, float] = {}
_seen_lock = threading.Lock()


def touch(name: str, *, now: float | None = None) -> None:
    """Note the user was seen; written at most once a minute per user, and
    never an error to the request that caused it."""
    now = time.time() if now is None else now
    with _seen_lock:
        if now - _seen.get(name, 0.0) < SEEN_EVERY_S:
            return
        _seen[name] = now
    try:
        p = _seen_path()
        try:
            seen = json.loads(p.read_bytes())
        except (OSError, ValueError):
            seen = {}
        seen[name] = _iso(dt.datetime.fromtimestamp(now, dt.timezone.utc))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".users-seen.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(seen, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def seen() -> dict[str, str]:
    try:
        return dict(json.loads(_seen_path().read_bytes()))
    except (OSError, ValueError):
        return {}


def listing() -> list[dict]:
    s = seen()
    return [u.public(s) for u in sorted(load(), key=lambda u: u.name)]
