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


def _stage(name: str) -> str:
    """The Dockerfile stage `AS name`, from its FROM line to the next."""
    text = (ROOT / "Dockerfile").read_text()
    stages = re.split(r"(?m)^(?=FROM )", text)
    found = [s for s in stages if re.match(rf"FROM \S+ AS {re.escape(name)}\s*\n", s)]
    assert len(found) == 1, f"no single stage {name!r}"
    return found[0]


def test_the_image_compiles_its_own_code_once_copied():
    """Louise's own modules are compiled at build (2026-11-02), after both
    COPYs and whole: a fresh container then imports bytecode instead of
    compiling ~150 modules first (~1 s a container, a tool run, a restart)."""
    base = _stage("base")
    lines = [ln.strip() for ln in base.splitlines()]
    at = [i for i, ln in enumerate(lines) if ln.startswith("RUN") and "compileall" in ln]
    assert at, "the image compiles nothing"
    line = lines[at[0]]
    assert " app" in line and " tools" in line and " -x " not in line, line
    assert lines.index("COPY app ./app") < at[0] and lines.index("COPY tools ./tools") < at[0]


# ---- the cdc extension (docs/notes/python-hot-spots.md, "D") ----

CDC = ROOT / "native" / "louise_cdc"


def _toml(p: Path) -> dict:
    import tomllib
    return tomllib.loads(p.read_text())


def test_the_cdc_extension_is_built_from_pinned_tools():
    """The image builds the `cdc` boundary loop's wheel in a stage of its
    own: the Rust image by exact version AND digest, maturin by version (the
    same in pyproject.toml), PyO3 by an exact requirement and every crate by
    the committed Cargo.lock, built --locked. Build-time only: none of them
    reaches the node."""
    build = _stage("cdc-build")
    first = build.splitlines()[0]
    assert re.fullmatch(r"FROM rust:\d+\.\d+\.\d+-slim-\w+@sha256:[0-9a-f]{64} AS cdc-build", first), first
    maturin = set(re.findall(r"maturin==([^\s\"']+)", build))
    assert len(maturin) == 1 and re.fullmatch(r"\d+\.\d+\.\d+", next(iter(maturin))), maturin
    assert _toml(CDC / "pyproject.toml")["build-system"]["requires"] == [f"maturin=={next(iter(maturin))}"]
    assert "maturin build --release --locked --out /wheels" in build
    pyo3 = _toml(CDC / "Cargo.toml")["dependencies"]["pyo3"]["version"]
    assert re.fullmatch(r"=\d+\.\d+\.\d+", pyo3), pyo3
    lock = _toml(CDC / "Cargo.lock")["package"]
    assert {p["name"]: p["version"] for p in lock}["pyo3"] == pyo3[1:]
    assert all(p.get("checksum") for p in lock if p["name"] != "louise_cdc")


def test_the_image_installs_the_built_wheel_under_the_constraints():
    """The wheel comes from the builder stage, at the version constraints.txt
    pins (pip refuses another), and never from PyPI: requirements.txt does
    not name it. And the image does not build unless app/chunking.py cuts
    through it and it cuts where the Python loop does."""
    base = _stage("base")
    assert "COPY --from=cdc-build /wheels /tmp/wheels" in base
    installs = re.findall(r"pip install[^\n]*louise_cdc[^\n]*", base)
    assert installs and all("-c constraints.txt" in i for i in installs), installs
    pins = {_name(ln): ln.split("==", 1)[1] for ln in _lines(ROOT / "constraints.txt")}
    assert pins["louise-cdc"] == _toml(CDC / "Cargo.toml")["package"]["version"]
    assert "louise-cdc" not in {_name(ln) for ln in _lines(ROOT / "requirements.txt")}
    lines = [ln.strip() for ln in base.splitlines()]
    check = [i for i, ln in enumerate(lines)
             if ln.startswith("RUN python -m tools.measure.cdc_identity --generated")]
    assert check and check[0] > lines.index("COPY tools ./tools")


def _stages(text: str) -> dict[str, list[str]]:
    """Each stage's instruction lines, continuations joined, by its name."""
    out: dict[str, list[str]] = {}
    name = None
    for ln in text.replace("\\\n", " ").splitlines():
        ln = ln.strip()
        if ln.startswith("FROM "):
            name = ln.split(" AS ")[-1].strip() if " AS " in ln else ln
            out[name] = [ln]
        elif name and ln and not ln.startswith("#"):
            out[name].append(ln)
    return out


def test_the_native_stage_compiles_checks_and_ships_only_its_files():
    """wikidata's textifier compiled by mypyc (docs/notes/python-hot-spots.md
    "E"): in a stage FROM base, with the pinned compiler, checked against its
    source before anything leaves; dev and runtime take its .so files and
    nothing else, so mypy and the C compiler never reach the node."""
    stages = _stages((ROOT / "Dockerfile").read_text())
    native = " ".join(stages["native"])
    assert stages["native"][0] == "FROM base AS native"
    assert "-c constraints.txt -r requirements-build.txt" in native
    assert "python -m tools.build_native --out /native" in native
    assert "python -m tools.measure.textify_identity --generated" in native
    assert list(stages)[-1] == "runtime", "the default build is the runtime"
    for stage in ("dev", "runtime"):
        froms = [ln for ln in stages[stage] if "--from=native" in ln]
        assert froms == ["COPY --from=native /native/ ./"], (stage, froms)
    for f in ("constraints.txt", "requirements.txt"):
        assert "mypy" not in {_name(ln) for ln in _lines(ROOT / f)}, f


def test_the_compiler_is_pinned_and_agrees_with_the_image():
    pins = {_name(ln): ln.split("==", 1)[1] for ln in _lines(ROOT / "constraints.txt")}
    build = _lines(ROOT / "requirements-build.txt")
    assert any(_name(ln) == "mypy" for ln in build)
    for ln in build:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.+\-]+", ln), ln
        if _name(ln) in pins:
            assert ln.split("==", 1)[1] == pins[_name(ln)], f"{_name(ln)}: the build and the image disagree"

