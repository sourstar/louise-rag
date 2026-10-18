"""app/rage/dxg.py: the card a process is on under WSL2's /dev/dxg, read
from the dxg build of amd-smi. The answers below are amd-smi 1.2.2's
(rocdxg-amd-smi-lib) on the dev PC's RX 9060 XT, 2026-10-18, trimmed."""
from __future__ import annotations

import json

import pytest

from app.rage import dxg, runs

STATIC = {"gpu_data": [{
    "gpu": 0,
    "bus": {"bdf": "0000:03:00.0", "max_pcie_width": 16, "slot_type": "PCIE"},
    "driver": {"name": "AMD Radeon RX 9060 XT",
               "version": "26.10.41.01-260811a-203304C-AMD-Software-Adrenalin-Edition",
               "os_kernel_version": "6.18.33.2-microsoft-standard-WSL2"}}]}
#: the backend's process is 1971; the driver lists its THREAD 1992
PROCESS = [{"gpu": 0, "process_list": [{"process_info": {
    "name": "ld.so", "pid": 1992,
    "memory_usage": {"gtt_mem": "N/A", "cpu_mem": "N/A", "vram_mem": "N/A"},
    "mem_usage": "N/A", "usage": {"gfx": "N/A", "enc": "N/A"}}}]}]


def fake_smi(process=PROCESS, static=STATIC):
    def run(args):
        if args[0] == "process":
            return json.dumps(process)
        if args[0] == "static":
            return json.dumps(static)
        raise AssertionError(args)
    return run


def thread(proc, tid, tgid):
    (proc / str(tid)).mkdir(parents=True)
    (proc / str(tid) / "status").write_text(f"Name:\tgrpcpp_sync_ser\nTgid:\t{tgid}\n"
                                            f"Pid:\t{tid}\n")


def test_the_driver_lists_threads_and_a_client_is_the_process_they_belong_to(tmp_path):
    thread(tmp_path, 1992, 1971)
    assert dxg.clients(fake_smi(), tmp_path) == {1971: "0000:03:00.0"}


def test_a_listed_thread_that_is_gone_is_no_client(tmp_path):
    assert dxg.clients(fake_smi(), tmp_path) == {}


def test_no_process_on_the_card_is_no_client(tmp_path):
    empty = [{"gpu": 0, "process_list": "No running processes detected"}]
    assert dxg.clients(fake_smi(process=empty), tmp_path) == {}


def test_a_process_listed_on_two_cards_is_neither_card(tmp_path):
    thread(tmp_path, 1992, 1971)
    thread(tmp_path, 1993, 1971)
    static = {"gpu_data": STATIC["gpu_data"] + [
        {"gpu": 1, "bus": {"bdf": "0000:04:00.0"}, "driver": {"version": "x"}}]}
    process = PROCESS + [{"gpu": 1, "process_list": [{"process_info": {"pid": 1993}}]}]
    assert dxg.clients(fake_smi(process=process, static=static), tmp_path) == {1971: "several"}


def test_the_driver_line_is_the_windows_driver_behind_the_card():
    assert dxg.driver_line(fake_smi(), "0000:03:00.0") == \
        "dxg 26.10.41.01-260811a-203304C-AMD-Software-Adrenalin-Edition"
    with pytest.raises(dxg.DxgUnreadable, match="no card at 0000:04:00.0"):
        dxg.driver_line(fake_smi(), "0000:04:00.0")


def test_an_answer_that_is_not_json_is_unreadable_never_guessed(tmp_path):
    with pytest.raises(dxg.DxgUnreadable, match="did not answer JSON"):
        dxg.clients(lambda args: "ERROR:root:Driver not initialized", tmp_path)


def test_amd_smi_that_fails_to_run_is_unreadable(tmp_path):
    run = dxg.runner(tmp_path / "bin" / "amd-smi")
    with pytest.raises(dxg.DxgUnreadable, match="amd-smi process"):
        run(["process", "--json"])


def test_the_driver_string_states_the_dxg_driver_in_place_of_a_bound_module(tmp_path):
    (tmp_path / "proc" / "sys" / "kernel").mkdir(parents=True)
    (tmp_path / "proc" / "sys" / "kernel" / "osrelease").write_text(
        "6.18.33.2-microsoft-standard-WSL2\n")
    # no /sys/bus/pci under WSL: without the line, the address is not a card
    with pytest.raises(runs.DriverUnknown):
        runs.driver_string("card", pci="0000:03:00.0", sys_root=tmp_path / "sys",
                           proc_root=tmp_path / "proc", runtime_lines=[])
    assert runs.driver_string(
        "card", pci="0000:03:00.0", sys_root=tmp_path / "sys", proc_root=tmp_path / "proc",
        runtime_lines=["hip-runtime-amd 7.2.53211.70201-81~24.04"],
        card_driver_line="dxg 26.10.41.01") == (
        "kernel 6.18.33.2-microsoft-standard-WSL2; dxg 26.10.41.01; "
        "hip-runtime-amd 7.2.53211.70201-81~24.04")
