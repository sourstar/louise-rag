"""VeGa node proof: the B70's memory, as Louise's GPU probe reads it
(app/gpu_probe.snapshot: the driver's total/used, every process's resident
VRAM and host-RAM GPU buffers from its DRM fdinfo). Run in a throwaway
container with --pid=host, /dev/dri, CAP_PERFMON and CAP_SYS_PTRACE.
Prints one JSON object."""
import json
import sys

from app import gpu_probe

PDEV = "0000:03:00.0"
MIB = 1 << 20
snap = gpu_probe.snapshot()
out = {"label": sys.argv[1] if len(sys.argv) > 1 else "", "notes": list(snap.notes)}
for d in snap.devices:
    if d.pdev != PDEV:
        continue
    out.update(total_mib=None if d.vram_total is None else d.vram_total // MIB,
               used_mib=None if d.vram_used is None else d.vram_used // MIB,
               attributed_mib=d.attributed // MIB)
    out["processes"] = [{"pid": p.pid, "container": p.container, "label": p.label,
                         "command": p.command, "vram_mib": p.vram // MIB,
                         "host_mib": p.host // MIB, "pushed_out": p.pushed_out}
                        for p in d.processes]
print(json.dumps(out, indent=1))
