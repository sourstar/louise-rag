"""A LocalAI that does what v4.11.0 does on `/v1/embeddings`, for VeGa's tests
(core/http/endpoints/openai/embeddings.go, core/backend/embeddings.go at
58830f7): wait for the backend's Health, load the model once with its
ModelOptions, then ONE gRPC `Embedding` call per input string, in order —
both WITHOUT a deadline, as LocalAI sets none; a
FINAL or unspecified layout passed through as the backend sent it; the floats
written as Go writes a float32 (gofloat.py), or as base64 of their little-
endian bytes for `encoding_format: base64`. A backend error is a 500 with Go's
`rpc error: code = … desc = …`.

`alter_input` and `alter_vector` make it the runner VeGa must not trust.
"""
from __future__ import annotations

import base64
import json
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import grpc
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response

from .gofloat import go_json


@dataclass
class Model:
    address: str
    options: dict[str, Any]
    alter_input: Callable[[str], str] | None = None
    alter_vector: Callable[[list[float]], list[float]] | None = None
    loaded: bool = field(default=False)


def _go_code(code: grpc.StatusCode) -> str:
    return "".join(w.capitalize() for w in code.name.split("_"))


def create(models: dict[str, Model], stubs: Any) -> FastAPI:
    pb, rpc = stubs.pb, stubs.rpc
    app = FastAPI()
    channels: dict[str, Any] = {}

    def stub(m: Model) -> Any:
        if m.address not in channels:
            channels[m.address] = rpc.BackendStub(grpc.insecure_channel(m.address))
        return channels[m.address]

    def error(e: grpc.RpcError) -> JSONResponse:
        message = f"rpc error: code = {_go_code(e.code())} desc = {e.details()}"
        return JSONResponse({"error": {"code": 500, "message": message, "type": ""}}, 500)

    @app.post("/v1/embeddings")
    def embeddings(body: dict[str, Any] = Body(...)):
        m = models[body["model"]]
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        s = stub(m)
        try:
            if not m.loaded:
                deadline = time.monotonic() + 30
                while True:                       # LocalAI waits for the backend first
                    try:
                        s.Health(pb.HealthMessage(), timeout=2)
                        break
                    except grpc.RpcError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.1)
                # NO deadline, as v4.11.0 (pkg/model/initializers.go: o.context)
                result = s.LoadModel(pb.ModelOptions(**m.options))
                if not result.success:
                    return JSONResponse({"error": {"code": 500, "message": result.message}}, 500)
                m.loaded = True
            vectors = []
            for text in inputs:
                if m.alter_input is not None:
                    text = m.alter_input(text)
                res = s.Embedding(pb.PredictOptions(Embeddings=text,          # no deadline
                                                    ModelIdentity=m.options.get("Model", "")))
                vector = list(res.embeddings)
                if m.alter_vector is not None:
                    vector = m.alter_vector(vector)
                vectors.append(vector)
        except grpc.RpcError as e:
            return error(e)
        if body.get("encoding_format") == "base64":
            items = [json.dumps(base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode())
                     for v in vectors]
        else:
            items = ["[" + ",".join(go_json(x) for x in v) + "]" for v in vectors]
        data = ",".join(f'{{"embedding":{e},"index":{i},"object":"embedding"}}'
                        for i, e in enumerate(items))
        text = (f'{{"created":{int(time.time())},"object":"list","id":"{uuid.uuid4()}",'
                f'"model":{json.dumps(body["model"])},"data":[{data}],'
                f'"usage":{{"prompt_tokens":0,"total_tokens":0}}}}')
        return Response(text, media_type="application/json")

    return app
