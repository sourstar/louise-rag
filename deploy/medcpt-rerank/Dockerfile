# MedCPT's cross-encoder on the Intel GPU through OpenVINO; server.py says
# why. The base is the Intel llama.cpp image the node's GPU encoders already
# run from: Ubuntu 24.04 with the Level Zero / OpenCL runtime that drives the
# node's GPU (Battlemage G21), in layers the node already holds.
#
#   docker build -t medcpt-rerank:ov deploy/medcpt-rerank
#   docker run --device /dev/dri -v <models>/medcpt-cross-ov:/models/medcpt-cross-ov:ro \
#     -p 8080:8080 medcpt-rerank:ov
#
# The model directory comes from convert.py beside this file (reranker-scope.md
# §12): openvino_model.xml/.bin (fp16 weights) and tokenizer.json.
FROM ghcr.io/ggml-org/llama.cpp:server-intel-b10630
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && rm -rf /var/lib/apt/lists/* \
 && pip3 install --no-cache-dir --break-system-packages \
      openvino==2026.3.1 tokenizers==0.22.2
COPY server.py /srv/medcpt-rerank/server.py
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
ENTRYPOINT ["python3", "/srv/medcpt-rerank/server.py"]
