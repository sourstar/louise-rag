"""The platform image is reproducible from a commit (2026-10-23): Perugia's
rebuild for 07b4a08 took tantivy 0.27.0 through `tantivy>=0.24` and its
Greek stemmer changed under the node's indexes. Every package the image
installs is pinned in constraints.txt to the version the gated image ran,
and the Dockerfile installs under it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _name(spec: str) -> str:
    return re.split(r"[\[<>=!~ ;]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _lines(p: Path) -> list[str]:
    return [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_every_constraint_is_an_exact_pin():
    for ln in _lines(ROOT / "constraints.txt"):
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.+\-]+", ln), ln


def test_every_requirement_is_constrained_and_agrees():
    pins = {_name(ln): ln.split("==", 1)[1] for ln in _lines(ROOT / "constraints.txt")}
    for ln in _lines(ROOT / "requirements.txt"):
        name = _name(ln)
        assert name in pins, f"{name} floats: add it to constraints.txt"
        if "==" in ln:
            assert ln.split("==", 1)[1].strip() == pins[name], f"{name}: requirements and constraints disagree"


def test_the_image_installs_under_the_constraints():
    docker = (ROOT / "Dockerfile").read_text()
    installs = re.findall(r"pip install[^\n]*-r requirements[^\n]*", docker)
    assert installs and all("-c constraints.txt" in i for i in installs), installs
