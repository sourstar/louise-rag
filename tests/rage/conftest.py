"""What the RAGE tests share. Until migration step 15g this was a whole
llama-swap harness; the LocalAI front's tests bring their own fake LocalAI
(tests/rage/localai_fake.py)."""
from __future__ import annotations

#: the digest a deployment pins in RAGE_IMAGE (09 §9.12)
IMAGE = "sha256:" + "ab" * 32
