"""The launcher of the platform's own model servers
(deploy/rage/servers/launch.py; migration-plan step 13b).

RAGE states a run from an argument vector with `${WEIGHTS}` and `${PORT}` in
it (docs/09-formats.md §9.3), and llama-swap expands `${PORT}` in a command
and not in an environment. The three servers read their settings from the
environment, so the launcher is what turns one into the other — in the same
process, so that /proc shows the template RAGE checks.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _launch():
    spec = importlib.util.spec_from_file_location(
        "rage_servers_launch", ROOT / "deploy" / "rage" / "servers" / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("server, device, variable, word", [
    ("dnabert2", "xpu", "MODEL_ID", "xpu"),
    ("dnabert2", "cpu", "MODEL_ID", "cpu"),
    ("madlad", "xpu", "MODEL_ID", "xpu"),
    ("medcpt", "xpu", "MODEL_DIR", "GPU"),          # OpenVINO's own names
    ("medcpt", "cpu", "MODEL_DIR", "CPU"),
])
def test_the_arguments_become_the_variables_each_server_reads(tmp_path, server, device,
                                                              variable, word):
    launch = _launch()
    weights = tmp_path / "model"
    weights.mkdir()
    args = launch.parse([server, "--weights", str(weights), "--device", device,
                         "--host", "127.0.0.1", "--port", "10007"])
    assert launch.environment(args) == {
        variable: str(weights), "DEVICE": word, "HOST": "127.0.0.1", "PORT": "10007",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


def test_absent_weights_are_an_error_that_names_the_path_never_a_download(tmp_path, capsys):
    launch = _launch()
    args = launch.parse(["madlad", "--weights", str(tmp_path / "not-here"), "--device", "xpu",
                         "--host", "127.0.0.1", "--port", "1"])
    with pytest.raises(SystemExit) as e:
        launch.environment(args)
    assert e.value.code == 2 and "not-here" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["--weights", "--device", "--host", "--port"])
def test_every_argument_is_required_so_the_vector_says_everything(missing, capsys):
    launch = _launch()
    full = {"--weights": "/m", "--device": "cpu", "--host": "127.0.0.1", "--port": "1"}
    argv = ["madlad"] + [x for k, v in full.items() if k != missing for x in (k, v)]
    with pytest.raises(SystemExit):
        launch.parse(argv)
    assert missing in capsys.readouterr().err


def test_the_launcher_imports_no_model_library():
    source = (ROOT / "deploy" / "rage" / "servers" / "launch.py").read_text(encoding="utf-8")
    top = source.split("def main", 1)[0]
    for library in ("torch", "transformers", "openvino"):
        assert f"import {library}" not in top


@pytest.mark.parametrize("server", ["madlad-translate", "medcpt-rerank"])
def test_the_servers_bind_the_host_they_are_given(server):
    """127.0.0.1 under RAGE, whose front is the only thing that reaches a
    model; every interface as an app of their own, as before."""
    source = (ROOT / "deploy" / server / "server.py").read_text(encoding="utf-8")
    assert 'os.getenv("HOST", "0.0.0.0")' in source
