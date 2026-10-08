"""VeGa node proof: start the wrapper as LocalAI does and send it the load
LocalAI would, to see what it says. Runs INSIDE LocalAI's container with the
package's own python:

    docker exec ix-localai-localai-1 sh -c 'cd /backends/vega-llama-cpp && \
      NEO_CACHE_DIR=/backends/vega-llama-cpp/neo-cache ./venv/bin/python /tmp/load_debug.py'
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, "/backends/vega-llama-cpp")
import grpc  # noqa: E402

import backend_pb2 as pb  # noqa: E402
import backend_pb2_grpc as rpc  # noqa: E402

PORT = int(os.environ.get("PORT", "50991"))
log = open("/tmp/vega-load-debug.log", "wb")
proc = subprocess.Popen(["/backends/vega-llama-cpp/run.sh", "--addr", f"127.0.0.1:{PORT}"],
                        cwd="/backends/vega-llama-cpp", stdout=log, stderr=subprocess.STDOUT)
stub = rpc.BackendStub(grpc.insecure_channel(f"127.0.0.1:{PORT}"))
t0 = time.monotonic()
while True:
    try:
        stub.Health(pb.HealthMessage(), timeout=2)
        break
    except grpc.RpcError as e:
        if time.monotonic() - t0 > 60:
            print("no health:", e.code(), e.details())
            break
        time.sleep(0.2)
print(f"health after {time.monotonic() - t0:.1f}s")
opts = pb.ModelOptions(
    Model="vega-weights/bge-m3-FP16.gguf", ModelFile="/models/vega-weights/bge-m3-FP16.gguf",
    ModelPath="/models", ContextSize=8192, Embeddings=True, NGPULayers=999, NBatch=8192,
    Threads=6, FlashAttention="on", MMap=True, Seed=0,
    Options=["parallel:1", "pooling:cls", "ubatch:8192"],
    EnvVars={"NEO_CACHE_DIR": "/backends/vega-llama-cpp/neo-cache"})
t1 = time.monotonic()
try:
    res = stub.LoadModel(opts, timeout=float(os.environ["LOAD_TIMEOUT"]) if os.environ.get("LOAD_TIMEOUT") else None)
    print(f"LoadModel after {time.monotonic() - t1:.1f}s: success={res.success} message={res.message!r}")
except grpc.RpcError as e:
    print(f"LoadModel failed after {time.monotonic() - t1:.1f}s: {e.code()} {e.details()}")
proc.terminate()
try:
    proc.wait(40)
except subprocess.TimeoutExpired:
    proc.kill()
log.close()
print(open("/tmp/vega-load-debug.log", "rb").read().decode(errors="replace")[-4000:])
