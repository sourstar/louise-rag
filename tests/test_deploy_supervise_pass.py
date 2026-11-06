"""deploy/supervise-pass.sh and deploy/docker-memory-ceiling.sh — a pass the
kernel kills comes back by itself (operator, 2026-10-30), and the caps of
what runs at once never make a GLOBAL OOM. Run against a fake `docker`."""
import os
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _fake_docker(tmp_path: Path, codes: list[int]) -> dict:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "codes").write_text("\n".join(map(str, codes)) + "\n")
    (bin_ / "docker").write_text(f"""#!/bin/bash
echo "$*" >> {tmp_path}/calls
code=$(head -1 {tmp_path}/codes); sed -i 1d {tmp_path}/codes
exit $code
""")
    (bin_ / "docker").chmod(0o755)
    return dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", PAUSE="0",
                HOLD=str(tmp_path / "hold"), CEILING=str(tmp_path / "no-ceiling"))


def _run(tmp_path, env, *args):
    p = subprocess.run(["bash", str(DEPLOY / "supervise-pass.sh"), "build", *args],
                       env=env, capture_output=True, text=True, timeout=60)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return p, calls


def test_a_kill_and_a_sigpipe_both_halve_the_pool_and_the_pass_runs_to_its_end(tmp_path):
    """München, 2026-10-30: run 2 died 141 (SIGPIPE: the pool's parent, a
    child OOM-killed) and run 3 started at 6 again."""
    env = _fake_docker(tmp_path, [137, 141, 1, 0])
    env["PARSE_PROCESSES"] = "12"
    p, calls = _run(tmp_path, env, "--memory", "12g", "img", "louise", "hold", "records", "build")
    assert p.returncode == 0, p.stdout + p.stderr
    procs = [next((a for a in c.split() if a.startswith("PARSE_PROCESSES=")), None) for c in calls]
    assert procs == ["PARSE_PROCESSES=12", "PARSE_PROCESSES=6", "PARSE_PROCESSES=3", "PARSE_PROCESSES=3"]


def test_a_pass_is_first_in_line_for_the_oom_killer_and_carries_its_cap(tmp_path):
    env = _fake_docker(tmp_path, [0])
    p, calls = _run(tmp_path, env, "-m", "4g", "img")
    assert p.returncode == 0
    assert "--oom-score-adj 500" in calls[0] and "-m 4g" in calls[0]


def test_a_pass_without_a_memory_cap_is_refused(tmp_path):
    env = _fake_docker(tmp_path, [0])
    p, calls = _run(tmp_path, env, "img", "louise", "hold", "check")
    assert p.returncode == 2 and not calls and "no --memory cap" in p.stdout


def test_a_held_pass_is_not_restarted(tmp_path):
    env = _fake_docker(tmp_path, [1, 0])
    (tmp_path / "bin" / "docker").write_text(f"""#!/bin/bash
echo "$*" >> {tmp_path}/calls; touch {tmp_path}/hold; exit 1
""")
    p, calls = _run(tmp_path, env, "--memory=8g", "img")
    assert p.returncode == 1 and len(calls) == 1 and "held" in p.stdout


@pytest.mark.parametrize("code", [130, 143])
def test_a_pass_stopped_on_purpose_is_not_restarted(tmp_path, code):
    """Operator, 2026-10-31: "if I stop it from freenas, likely because I want
    to fix something that might actually lead to the crash, it shouldn't
    restart by itself". A `docker stop` is SIGTERM: the pass exits 130 (07
    §7.7), or 143 if it did not catch it. The OOM killer's SIGKILL (137)
    still brings it back."""
    env = _fake_docker(tmp_path, [code, 0])
    p, calls = _run(tmp_path, env, "--memory=8g", "img")
    assert p.returncode == code and len(calls) == 1 and "stopped on purpose" in p.stdout
    oom = tmp_path / "oom"
    oom.mkdir()
    p, calls = _run(oom, _fake_docker(oom, [137, 0]), "--memory=8g", "img")
    assert p.returncode == 0 and len(calls) == 2, "an OOM kill comes back by itself"


def test_a_refused_pass_is_not_restarted_and_a_deferred_one_is(tmp_path):
    """07 §7.7: exit 2 is a refusal — a rule stood in the way, and nothing
    changes by itself — exit 3 a deferral that may clear. München,
    2026-11-02: pubmed's CPU reference was refused every minute (VeGa states
    no run for an unmeasured tree) and held the night's chain."""
    env = _fake_docker(tmp_path, [2, 0])
    p, calls = _run(tmp_path, env, "--memory=8g", "img")
    assert p.returncode == 2 and len(calls) == 1 and "refused" in p.stdout
    later = tmp_path / "deferred"
    later.mkdir()
    p, calls = _run(later, _fake_docker(later, [3, 0]), "--memory=8g", "img")
    assert p.returncode == 0 and len(calls) == 2, "a deferral is asked again"


def _cgroup(tmp_path, *, max_="max", current=20 << 30, total_kb=61_900_000):
    cg = tmp_path / "docker"
    cg.mkdir()
    (cg / "memory.max").write_text(max_ + "\n")
    (cg / "memory.current").write_text(f"{current}\n")
    (tmp_path / "meminfo").write_text(f"MemTotal:       {total_kb} kB\nMemFree: 1 kB\n")
    return dict(os.environ, DOCKER_CGROUP=str(cg), MEMINFO=str(tmp_path / "meminfo")), cg


def _ceiling(env):
    return subprocess.run(["bash", str(DEPLOY / "docker-memory-ceiling.sh")], env=env,
                          capture_output=True, text=True, timeout=30)


def test_the_ceiling_is_ninety_percent_of_the_host_in_whole_pages_and_idempotent(tmp_path):
    env, cg = _cgroup(tmp_path)
    p = _ceiling(env)
    want = 61_900_000 * 1024 * 9 // 10 // 4096 * 4096
    assert p.returncode == 0 and (cg / "memory.max").read_text().strip() == str(want), p.stdout
    assert "max -> " in p.stdout
    again = _ceiling(env)
    assert again.returncode == 0 and again.stdout == "", "set already: silent"


def test_a_ceiling_under_what_the_containers_use_is_not_set(tmp_path):
    env, cg = _cgroup(tmp_path, current=58 << 30)
    p = _ceiling(env)
    assert p.returncode == 1 and (cg / "memory.max").read_text().strip() == "max" and "NOT set" in p.stdout


def test_no_docker_cgroup_is_said_and_is_not_an_error(tmp_path):
    env = dict(os.environ, DOCKER_CGROUP=str(tmp_path / "absent"), PATH="/usr/bin:/bin")
    p = _ceiling(env)
    assert p.returncode == 0 and "not set" in p.stdout


def test_the_supervisor_applies_the_ceiling_before_every_run(tmp_path):
    env = _fake_docker(tmp_path, [137, 0])
    ceiling = tmp_path / "ceiling.sh"
    ceiling.write_text(f"#!/bin/bash\necho x >> {tmp_path}/ceilings\n")
    ceiling.chmod(0o755)
    env["CEILING"] = str(ceiling)
    p, calls = _run(tmp_path, env, "--memory", "12g", "img")
    assert p.returncode == 0 and len(calls) == 2
    assert (tmp_path / "ceilings").read_text().count("x") == 2
