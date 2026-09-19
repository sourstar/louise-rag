"""`rage.toml` — the one desired state of the node's model container.

docs/notes/rage.md §5. Tier 2 (CLAUDE.md invariant 10): a file the operator
edits, applied at start and whenever it changes. No model, no placement and
no preload list is ever kept in a database.

TWO PLACES, ONE CONFIGURATION. `rage.toml` is the operator's, written by
hand and commented; this module never rewrites it. A model added through
the API (`PUT /louise/models/<id>`, the System page's form) is written whole
to `rage.d/<id>.toml`, a file the front owns. An id defined in both is an
error, and `DELETE` refuses a model that lives in `rage.toml`: rewriting the
operator's file would drop the comments that say why each flag is there.

ONE COMPLETE COMMAND PER DEVICE. `cmd.xpu` and `cmd.cpu` are each a whole
argument vector, with the weights written `${WEIGHTS}` and the port
`${PORT}` and nothing else that names the host. That template IS the
engine's `args` (docs/09-formats.md §9.3), so the device that ran decides
the run with nothing inferred, and a device change is a new run the gate
tests by itself (03 §3.3).
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

CPU = "cpu"
WEIGHTS = "${WEIGHTS}"
PORT = "${PORT}"

_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_DEVICE = re.compile(r"[a-z][a-z0-9]{0,15}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class RageConfigError(ValueError):
    """The configuration is not valid. The message names the entry and the
    rule, never a guess at what was meant."""


@dataclass(frozen=True)
class EngineDecl:
    """The readable half of an engine (09 §9.3): what serves the model, as
    its software names itself. Declared, and bound by nothing but the image
    and the reference vectors."""
    name: str
    build: str
    variant: str | None = None


@dataclass(frozen=True)
class ModelEntry:
    id: str
    engine: EngineDecl
    weights: str                          # an absolute path: a file, or a directory
    device: str                           # the default placement
    memory: Mapping[str, int]             # MiB the model holds, per device — measured
    cmd: Mapping[str, tuple[str, ...]]    # per device, the whole argument vector
    env: Mapping[str, str] = field(default_factory=dict)
    health: str = "/health"
    source: str = ""                      # the file that defines it
    #: MiB of HOST RAM the model holds while it is on a card, per card. A
    #: model on the GPU is not free on the host: measured on the node,
    #: 2026-09-19, the eight GPU servers held 6.1 GiB between them at rest
    #: and 22.0 GiB at their load-time peaks (MADLAD alone 8.5).
    host: Mapping[str, int] = field(default_factory=dict)

    @property
    def devices(self) -> tuple[str, ...]:
        return tuple(sorted(self.cmd))

    def host_need(self, device: str) -> int:
        """The host RAM this placement takes: all of it on the CPU, the
        declared share of it on a card."""
        return self.memory[device] if device == CPU else self.host[device]

    def variant_id(self, device: str) -> str:
        """The id llama-swap knows this model on this device by. Internal:
        a collection's URL names the model, never the device."""
        return f"{self.id}@{device}"

    def template(self, device: str) -> tuple[str, ...]:
        try:
            return self.cmd[device]
        except KeyError:
            raise RageConfigError(f"model {self.id!r} declares no command for device "
                                  f"{device!r}; it has {list(self.devices)}") from None


@dataclass(frozen=True)
class RageConfig:
    models: Mapping[str, ModelEntry]
    preload: tuple[str, ...] = ()
    reserve_mib: Mapping[str, int] = field(default_factory=dict)
    #: a device's capacity, where it cannot be read from the hardware
    capacity_mib: Mapping[str, int] = field(default_factory=dict)
    #: a device as its driver names it (09 §9.3's `device`), where declared
    device_names: Mapping[str, str] = field(default_factory=dict)


# ---- reading ---------------------------------------------------------------

def _table(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RageConfigError(f"{where} must be a table")
    return raw


def _int_map(raw: Any, where: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for device, value in _table(raw, where).items():
        if not _DEVICE.fullmatch(device):
            raise RageConfigError(f"{where}: {device!r} is not a device name")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RageConfigError(f"{where}.{device} must be a whole number of MiB, "
                                  f"got {value!r}")
        out[device] = value
    return out


def _template(raw: Any, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(a, str) and a for a in raw):
        raise RageConfigError(f"{where} must be a non-empty list of strings: the whole "
                              f"argument vector, program first")
    if not raw[0].startswith("/"):
        raise RageConfigError(f"{where}: the program {raw[0]!r} must be an absolute path")
    for placeholder in (WEIGHTS, PORT):
        if raw.count(placeholder) != 1:
            raise RageConfigError(f"{where} must carry {placeholder} exactly once, as an "
                                  f"argument of its own (09 §9.3); it has "
                                  f"{raw.count(placeholder)}")
    for arg in raw:
        if "${" in arg and arg not in (WEIGHTS, PORT):
            raise RageConfigError(f"{where}: {arg!r} — only {WEIGHTS} and {PORT} are "
                                  f"placeholders, each an argument of its own")
    return tuple(raw)


def entry_from_table(model_id: str, raw: Any, *, source: str = "") -> ModelEntry:
    """One `[models.<id>]` table — from a file, or the body of a PUT."""
    where = f"models.{model_id}"
    if not _ID.fullmatch(model_id):
        raise RageConfigError(f"{model_id!r} is not a model id: lower-case letters, "
                              f"digits, '.', '_' and '-', and never '@' or '/'")
    t = _table(raw, where)
    unknown = set(t) - {"engine", "weights", "device", "memory", "host", "cmd", "env",
                        "health"}
    if unknown:
        raise RageConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    for key in ("engine", "weights", "device", "memory", "cmd"):
        if key not in t:
            raise RageConfigError(f"{where}: missing {key!r}")

    e = _table(t["engine"], f"{where}.engine")
    if set(e) - {"name", "build", "variant"}:
        raise RageConfigError(f"{where}.engine: unknown key(s) "
                              f"{sorted(set(e) - {'name', 'build', 'variant'})}")
    for key in ("name", "build"):
        if not isinstance(e.get(key), str) or not e[key]:
            raise RageConfigError(f"{where}.engine.{key} must be a non-empty string")
    variant = e.get("variant")           # absent is null: TOML has no null
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise RageConfigError(f"{where}.engine.variant must be a non-empty string, "
                              f"or absent")

    weights = t["weights"]
    if not isinstance(weights, str) or not weights.startswith("/"):
        raise RageConfigError(f"{where}.weights must be an absolute path")

    cmd = {device: _template(argv, f"{where}.cmd.{device}")
           for device, argv in _table(t["cmd"], f"{where}.cmd").items()}
    if not cmd:
        raise RageConfigError(f"{where}.cmd declares no device")
    for device in cmd:
        if not _DEVICE.fullmatch(device):
            raise RageConfigError(f"{where}.cmd: {device!r} is not a device name")

    device = t["device"]
    if device not in cmd:
        raise RageConfigError(f"{where}.device = {device!r}, which has no command; "
                              f"it has {sorted(cmd)}")

    memory = _int_map(t["memory"], f"{where}.memory")
    for d in cmd:
        if not memory.get(d):
            raise RageConfigError(f"{where}.memory.{d} is missing or zero: admission "
                                  f"cannot place a model whose size it does not know")

    host = _int_map(t.get("host", {}), f"{where}.host")
    if CPU in host:
        raise RageConfigError(f"{where}.host.cpu: on the CPU the host RAM a model holds "
                              f"IS memory.cpu; host is for the cards")
    for d in sorted(set(cmd) - {CPU}):
        if not host.get(d):
            raise RageConfigError(
                f"{where}.host.{d} is missing or zero: the host RAM, in MiB, the model "
                f"holds while it is on {d}. A model on a card is not free on the host — "
                f"load it and read `GET /louise/models`, which shows what it holds")
    if set(host) - set(cmd):
        raise RageConfigError(f"{where}.host names {sorted(set(host) - set(cmd))}, which "
                              f"have no command")

    env = _table(t.get("env", {}), f"{where}.env")
    for name, value in env.items():
        if not _ENV_NAME.fullmatch(name):
            raise RageConfigError(f"{where}.env: {name!r} is not a variable name")
        if not isinstance(value, str):
            raise RageConfigError(f"{where}.env.{name} must be a string")
        if "${" in value:
            raise RageConfigError(f"{where}.env.{name}: no placeholder is expanded in the "
                                  f"environment; the port and the weights go in cmd")

    health = t.get("health", "/health")
    if not isinstance(health, str) or not health.startswith("/"):
        raise RageConfigError(f"{where}.health must be a path starting with '/'")

    return ModelEntry(id=model_id, engine=EngineDecl(e["name"], e["build"], variant),
                      weights=weights, device=device, memory=memory, cmd=cmd,
                      env=dict(env), health=health, source=source, host=host)


def _models(doc: dict[str, Any], source: str) -> dict[str, ModelEntry]:
    return {model_id: entry_from_table(model_id, table, source=source)
            for model_id, table in _table(doc.get("models", {}), "models").items()}


def load(path: Path) -> RageConfig:
    """`rage.toml`, and beside it every `rage.d/<id>.toml`."""
    path = Path(path)
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RageConfigError(f"cannot read {path}: {type(e).__name__}") from None
    except tomllib.TOMLDecodeError as e:
        raise RageConfigError(f"{path}: {e}") from None

    unknown = set(doc) - {"rage", "models"}
    if unknown:
        raise RageConfigError(f"{path}: unknown table(s) {sorted(unknown)}")
    head = _table(doc.get("rage", {}), "rage")
    unknown = set(head) - {"preload", "reserve_mib", "capacity_mib", "device_names"}
    if unknown:
        raise RageConfigError(f"{path}: [rage] has unknown key(s) {sorted(unknown)}")

    models = _models(doc, str(path))
    for extra in sorted(dropin_dir(path).glob("*.toml")):
        try:
            more = _models(tomllib.loads(extra.read_text(encoding="utf-8")), str(extra))
        except tomllib.TOMLDecodeError as e:
            raise RageConfigError(f"{extra}: {e}") from None
        if list(more) != [extra.stem]:
            raise RageConfigError(f"{extra} must define exactly the model "
                                  f"{extra.stem!r}; it defines {sorted(more)}")
        if extra.stem in models:
            raise RageConfigError(f"model {extra.stem!r} is defined twice: in "
                                  f"{models[extra.stem].source} and in {extra}")
        models.update(more)

    preload = head.get("preload", [])
    if not isinstance(preload, list) or not all(isinstance(p, str) for p in preload):
        raise RageConfigError("rage.preload must be a list of model ids")
    for model_id in preload:
        if model_id not in models:
            raise RageConfigError(f"rage.preload names {model_id!r}, which is not a model")

    names = _table(head.get("device_names", {}), "rage.device_names")
    for device, name in names.items():
        if not isinstance(name, str) or not name:
            raise RageConfigError(f"rage.device_names.{device} must be a non-empty string")
    capacity = _int_map(head.get("capacity_mib", {}), "rage.capacity_mib")
    # A CARD IS DECLARED, NEVER PROBED. Its name is 09 §9.3's `device`, "as
    # its driver names it", and enters every run identity; its size is what
    # admission places models against. Neither changes while the card is in
    # the machine, and reading either would mean importing the GPU probe,
    # which sees the host's processes and is imported by nothing.
    for card in sorted({d for m in models.values() for d in m.cmd} - {CPU}):
        if card not in names:
            raise RageConfigError(
                f"declare [rage] device_names.{card}: the card as its driver names it, "
                f"e.g. \"Intel(R) Arc(TM) Pro B70 Graphics\" (09 §9.3's `device`)")
        if not capacity.get(card):
            raise RageConfigError(
                f"declare [rage] capacity_mib.{card}: the card's memory in MiB, which "
                f"admission places the models against")
    return RageConfig(models=models, preload=tuple(preload),
                      reserve_mib=_int_map(head.get("reserve_mib", {}), "rage.reserve_mib"),
                      capacity_mib=capacity, device_names=dict(names))


def dropin_dir(config_path: Path) -> Path:
    return Path(config_path).parent / "rage.d"


# ---- writing (the API's models only) ---------------------------------------

def _s(value: str) -> str:
    """A TOML basic string. JSON's escapes are a subset of TOML's."""
    return json.dumps(value, ensure_ascii=False)


def entry_to_toml(entry: ModelEntry) -> str:
    """The whole of `rage.d/<id>.toml`. Written by the front and by nothing
    else, so it carries no comment worth keeping."""
    e = entry.engine
    engine = f"name = {_s(e.name)}, build = {_s(e.build)}"
    if e.variant is not None:
        engine += f", variant = {_s(e.variant)}"
    lines = [f"# written by RAGE (PUT /louise/models/{entry.id}); edit through the API",
             f"[models.{_s(entry.id)}]",
             f"engine  = {{ {engine} }}",
             f"weights = {_s(entry.weights)}",
             f"device  = {_s(entry.device)}",
             "memory  = { " + ", ".join(f"{d} = {entry.memory[d]}"
                                        for d in sorted(entry.memory)) + " }",
             f"health  = {_s(entry.health)}"]
    if entry.host:
        lines.append("host    = { " + ", ".join(f"{d} = {entry.host[d]}"
                                                for d in sorted(entry.host)) + " }")
    if entry.env:
        lines.append("env     = { " + ", ".join(f"{k} = {_s(v)}"
                                                for k, v in sorted(entry.env.items())) + " }")
    for device in entry.devices:
        lines.append(f"cmd.{device} = [" + ", ".join(_s(a) for a in entry.cmd[device]) + "]")
    return "\n".join(lines) + "\n"


def entry_to_json(entry: ModelEntry) -> dict[str, Any]:
    return {"engine": {"name": entry.engine.name, "build": entry.engine.build,
                       "variant": entry.engine.variant},
            "weights": entry.weights, "device": entry.device,
            "memory": dict(entry.memory), "host": dict(entry.host), "env": dict(entry.env),
            "health": entry.health,
            "cmd": {d: list(entry.cmd[d]) for d in entry.devices}}
