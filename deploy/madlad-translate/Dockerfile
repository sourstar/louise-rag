# MADLAD-400-3B-MT translation server on the Intel Arc GPU (server.py says
# why). The runtime recipe is deploy/dnabert2-xpu's, verified on this card:
# the kobuk-team PPA's compute runtime (Intel's own repo ships one too old
# for the B70 and enumerates zero devices) and torch from the XPU index —
# no intel-extension-for-pytorch, XPU is built into torch since 2.5.
#
#   docker build -t madlad-translate:xpu deploy/madlad-translate
#   docker run --device /dev/dri -v <models>/madlad-models:/data -p 8080:8080 \
#     madlad-translate:xpu
#
# The weights (11.8 GB, fp32 safetensors, loaded as bf16) download into
# /data on the first start; mount it so they download once.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates \
        python3 python3-venv \
 && add-apt-repository -y ppa:kobuk-team/intel-graphics \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        intel-opencl-icd libze-intel-gpu1 libze1 \
 && apt-get purge -y software-properties-common \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# TRANSFORMERS 4.46.3, PINNED. 5.17 loads this checkpoint wrongly and says
# so only as a warning ("both are present in the checkpoints with different
# values, so we will NOT tie them"): the decoder then keeps an embedding the
# 4.x models never used, and every translation came back as `ě ě ě …`
# (node, 2026-09-18). 4.46.3 ties them and translates.
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/xpu \
 && pip install --no-cache-dir "transformers==4.46.3" accelerate sentencepiece protobuf

ENV HF_HOME=/data DEVICE=xpu PYTHONUNBUFFERED=1
COPY server.py /srv/madlad-translate/server.py
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=900s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
ENTRYPOINT ["python3", "/srv/madlad-translate/server.py"]
