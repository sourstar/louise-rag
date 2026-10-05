"""`rage.toml`: the cards, and nothing of the lifecycle (migration
step 15, docs/notes/rage-over-localai.md §7).

It shrinks to what the run identity needs and nothing of the lifecycle: the
cards, each as its driver names it (09 §9.3's `device`) and by its PCI
address. The address does two jobs: the kernel driver stated in a run is the
one THAT card is bound to (runs.driver_string), and a model is placed on the
card whose address its process's DRM clients report (`drm-pdev`) — read,
never declared. Models are LocalAI's own YAML; a `[models]` table here was the
llama-swap front's (retired at 15g), and is refused so that an old file says so.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .model import CPU, RageConfigError

_PCI = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}.[0-7]")
_DEVICE = re.compile(r"[a-z][a-z0-9]{0,15}")


@dataclass(frozen=True)
class LocalAIConfig:
    device_names: Mapping[str, str]     # card -> as its driver names it
    pci: Mapping[str, str]              # card -> its PCI address

    def card_at(self, pdev: str) -> str | None:
        return next((card for card, address in self.pci.items() if address == pdev), None)


def load(path: Path) -> LocalAIConfig:
    path = Path(path)
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RageConfigError(f"cannot read {path}: {type(e).__name__}") from None
    except tomllib.TOMLDecodeError as e:
        raise RageConfigError(f"{path}: {e}") from None
    if "models" in doc:
        raise RageConfigError(
            f"{path}: [models] was the llama-swap front's, retired at migration step 15g. The models "
            f"are LocalAI's YAML (PUT /louise/models/<id>), and rage.toml declares only the "
            f"cards: [rage] device_names and pci")
    unknown = set(doc) - {"rage"}
    if unknown:
        raise RageConfigError(f"{path}: unknown table(s) {sorted(unknown)}")
    head = doc.get("rage", {})
    if not isinstance(head, dict):
        raise RageConfigError(f"{path}: [rage] is a table")
    unknown = set(head) - {"device_names", "pci"}
    if unknown:
        raise RageConfigError(f"{path}: [rage] has unknown key(s) {sorted(unknown)}; "
                              f"it declares device_names and pci only")
    names, pci = head.get("device_names", {}), head.get("pci", {})
    if not isinstance(names, dict) or not isinstance(pci, dict):
        raise RageConfigError(f"{path}: [rage] device_names and pci are tables")
    for card in sorted(set(names) | set(pci)):
        if card == CPU or not _DEVICE.fullmatch(card):
            raise RageConfigError(f"{path}: {card!r} is not a card's name")
        name, address = names.get(card), pci.get(card)
        if not isinstance(name, str) or not name:
            raise RageConfigError(f"declare [rage] device_names.{card}: the card as its "
                                  f"driver names it (09 §9.3's `device`)")
        if not isinstance(address, str) or not _PCI.fullmatch(address):
            raise RageConfigError(f"declare [rage] pci.{card}: its address as `lspci -D` "
                                  f"prints it, like \"0000:03:00.0\"; got {address!r}")
    if len(set(pci.values())) != len(pci):
        raise RageConfigError(f"{path}: two cards declare one PCI address")
    return LocalAIConfig(device_names=dict(names), pci=dict(pci))
