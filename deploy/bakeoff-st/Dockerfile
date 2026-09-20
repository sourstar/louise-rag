# A sentence-transformers image for the bake-off's local rows, and nothing
# else. Separate from louise-rag:dev on purpose: invariant 7 keeps torch out
# of the app, and the dev image is what proves that — adding torch to it to
# run one comparison would quietly remove the proof.
#
# CPU torch, from the CPU index: this measures a model's retrieval quality,
# which is a property of the weights, not of the device. The node's card is
# for the models the node serves.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
      torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu

# Pinned: the model card records sentence-transformers 5.1.0 and
# transformers 4.55.4 as what produced the published numbers this run is
# checked against. A different major could pool or truncate differently and
# the check would be meaningless.
RUN pip install --no-cache-dir \
      "sentence-transformers==5.1.0" \
      "transformers==4.55.4" \
      "pyarrow==25.0.1" \
      numpy einops

ENV PYTHONPATH=/srv HF_HOME=/hf
WORKDIR /srv
