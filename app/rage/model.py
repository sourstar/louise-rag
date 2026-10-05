"""What every part of RAGE calls a device, a model id and a configuration
error.

Until migration step 15g this module was `rage.toml`'s whole desired state:
each model's command per device, its memory, priority, preload and park
(docs/notes/rage.md §5). LocalAI's model YAML holds the lifecycle now
(deploy/rage/localai-models/; rage-over-localai §7), and `rage.toml` holds the
cards alone (app/rage/localai_config.py). The retirement is recorded in
docs/notes/legacy-to-delete.md §8.
"""
from __future__ import annotations

import re

CPU = "cpu"

#: a model's id: the last segment of `/upstream/<id>/…`, LocalAI's model name
#: and its YAML's file name (07 §7.8)
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class RageConfigError(ValueError):
    """The configuration is not valid. The message names the entry and the
    rule, never a guess at what was meant."""
