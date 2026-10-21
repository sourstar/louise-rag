# The NVIDIA row on a rented Azure GPU

One session, about 2–3 hours of VM, to measure an NVIDIA card for the
cross-vendor deviation reference (docs/notes/approved-encoders.md, operator
2026-10-21). No Louise node runs on the VM: only LocalAI, VeGa's wrapper and
receipts, and `tools/cross_vendor_check.py` against a space exported from the
node. München drives it over SSH from the operator's computer; this file is
the order. Each step says what must be true before the next.

| | step | time |
|---|---|---|
| 0 | quota (once) | hours to days, before the session |
| 1 | the VM | 5 min |
| 2 | driver, Docker, NVIDIA Container Toolkit | 20 min |
| 3 | sources, images | 15 min |
| 4 | weights, checked by sha256 | 10–20 min |
| 5 | LocalAI, its CUDA backend, VeGa's wrapper | 15 min |
| 6 | runs stated, bit-exact over fresh loads | 15 min |
| 7 | the space exported from the node, measured | 30–45 min |
| 8 | the result home, the VM deleted | 5 min |

## 0. Quota (once, ahead)

New subscriptions have **0** vCPUs for GPU families. Azure portal → *Quotas* →
*Compute* → the region (one near the operator: *Germany West Central* or *West
Europe*) → **Standard NCASv3_T4 Family vCPUs** → request **8**. Check that the
region offers `Standard_NC8as_T4_v3`:

```
az vm list-skus -l germanywestcentral --size Standard_NC8as_T4_v3 -o table
```

Why this size: one **Tesla T4** passed through whole (not a vGPU slice, not
MIG: the wrapper refuses a MIG card), Turing 7.5 (CUDA 13's floor), 16 GB,
and 8 vCPUs for the CPU twin. About €0.70–0.80 an hour.

## 1. The VM

```
az group create -n louise-nv -l germanywestcentral
az vm create -g louise-nv -n louise-nv --size Standard_NC8as_T4_v3 \
  --image Canonical:ubuntu-24_04-lts:server:latest --os-disk-size-gb 128 \
  --security-type Standard \
  --admin-username louise --ssh-key-values ~/.ssh/id_ed25519.pub \
  --public-ip-sku Standard --nsg-rule SSH
# SSH from the operator's address only; nothing else is reachable from outside
az network nsg rule update -g louise-nv --nsg-name louise-nvNSG -n default-allow-ssh \
  --source-address-prefixes "$(curl -s https://ifconfig.me)/32"
# a safety net: the VM stops itself at 22:00 UTC if it is forgotten
az vm auto-shutdown -g louise-nv -n louise-nv --time 2200
```

`--security-type Standard`: Trusted launch (the CLI's and the portal's
default) turns on Secure Boot and vTPM, and Microsoft's N-series driver guide
says the NVIDIA driver install hangs with them on. No availability zone: the
T4 sizes in Germany West Central are offered without one.

From the portal instead (operator, 2026-10-21): Basics → *Availability
options* "No infrastructure redundancy required", *Security type* "Standard",
size `Standard_NC8as_T4_v3`, SSH public key; Disks → 128 GiB Standard SSD,
*Delete with VM*. Then Networking → the SSH inbound rule → *Source* "My IP
address". The names the portal chose (user `azureuser`, resource group
`<vm>_group`) stand for `louise` and `louise-nv` below; step 8 deletes that
resource group, which takes its IP and NIC with it.

LocalAI (30286) and VeGa (30439) are published on the VM's own interfaces
only; the network security group admits SSH alone. Every command below runs on
the VM (`ssh louise@<ip>`).

## 2. Driver, Docker, NVIDIA Container Toolkit

```
sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common
sudo ubuntu-drivers list --gpgpu           # pick the newest 580 or later server driver
sudo apt-get install -y nvidia-driver-580-server nvidia-utils-580-server
sudo reboot
nvidia-smi                                  # Tesla T4, driver 580 or newer, MIG: N/A
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker louise
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit rsync
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
newgrp docker
docker run --rm --gpus all ubuntu nvidia-smi   # the card, seen from a container
```

Done when: `nvidia-smi` in a container lists the T4.

## 3. Sources and images

The repository at the commit being measured, from the operator's computer
(the repository is private: an archive, not a clone):

```
# operator's computer, in the repo
git archive --format=tar.gz -o louise-src.tgz <main sha>
scp louise-src.tgz louise@<ip>:
# VM
mkdir -p ~/louise && tar -xzf ~/louise-src.tgz -C ~/louise && cd ~/louise
echo <main sha> > REVISION
docker build -t louise-rag:local .          # VeGa's receipts service and the tools
```

LocalAI's CUDA 13 image is pulled by the compose file in step 5 (pinned by
digest).

## 4. Weights, byte-identical to the node's

The models the space was calibrated with, at the paths the YAMLs name
(`weights/<…>` under `LOUISE_WEIGHTS`). The node states their sha256:

```
# node (München): the reference digests
sha256sum /mnt/ssd5/apps/models/llama-embed/Qwen3-Embedding-4B-Q4_K_M.gguf \
          /mnt/ssd5/apps/models/bge-m3/bge-m3-FP16.gguf
```

On the VM, from the Hugging Face revisions whose bytes are the node's
(München matched each to the Hub's sha256, 2026-10-21):

```
sudo mkdir -p /data/weights/llama-embed /data/weights/bge-m3 && sudo chown -R louise /data/weights
HF=https://huggingface.co
curl -fL -o /data/weights/llama-embed/Qwen3-Embedding-4B-Q4_K_M.gguf \
  $HF/Qwen/Qwen3-Embedding-4B-GGUF/resolve/f4602530db1d980e16da9d7d3a70294cf5c190be/Qwen3-Embedding-4B-Q4_K_M.gguf
curl -fL -o /data/weights/bge-m3/bge-m3-FP16.gguf \
  $HF/gpustack/bge-m3-GGUF/resolve/2d48f1737679ad900d5c26c5aad5410e9c70fdca/bge-m3-FP16.gguf
sha256sum -c <<'SUMS'
2b0cf8f17b4c723c27303015383c27ec4bf2d8314bb677d05e920dd70bb0f16b  /data/weights/llama-embed/Qwen3-Embedding-4B-Q4_K_M.gguf
daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c  /data/weights/bge-m3/bge-m3-FP16.gguf
SUMS
```

Qwen3-Embedding-4B Q4_K_M (2,496,703,776 B) is gutenberg's
`space.encoder.weights`; bge-m3 F16 is 1,157,671,200 B. Done when both say
`OK`. A digest that differs is a different model: stop, and copy that file
from the node instead (`scp`, through the operator's computer).

## 5. LocalAI, its CUDA backend, VeGa's wrapper

```
cd ~/louise
LOUISE_WEIGHTS=/data/weights docker compose -f deploy/localai-nvidia/compose.yaml up -d
docker exec louise-localai-localai-1 ./local-ai backends install llama-cpp   # → cuda13-llama-cpp
docker exec louise-localai-localai-1 nvidia-smi --query-gpu=pci.bus_id,name --format=csv,noheader
docker exec louise-localai-localai-1 sh -c 'command -v nvidia-smi'
```

Then the wrapper package, from the same commit, as `README.md` step 4 says:
`CARDS='{"<bus id as 0000:xx:00.0, lower case>": "Tesla T4"}'`,
`NVIDIA_SMI=<the path above>`, `PKG=vega-llama-cpp BACKEND=cuda13-llama-cpp`,
`SOCKET=/backends/vega-llama-cpp/run/receipts.sock`, `OWNER=0:0`. Restart:
`docker compose -f deploy/localai-nvidia/compose.yaml restart localai`.

## 6. Runs stated, bit-exact

```
L="docker run --rm --network host -e VEGA_URL=http://localhost:30439 louise-rag:local"
for m in vega-qwen3-embedding-4b vega-qwen3-embedding-4b-cpu vega-bge-m3; do
  $L python -m app.louise hold embedder run --encoder http://localhost:30286 --api vega --model $m
done
```

Each states a run: the card ones with `device` "Tesla T4", the driver
`nvidia 580.…` and the CUDA libraries the backend mapped (`libcublas…
sha256:…`); the twin with `device` "cpu". A refusal names its reason (the
contract, a split load, MIG): fix that and repeat. Then invariant 12,
measured:

```
for m in vega-qwen3-embedding-4b vega-bge-m3; do
  $L python -m tools.vega_repro_check --url http://localhost:30286 --model $m \
     --n 16 --loads 3 --repeats 2
done
```

Done when every text is bit-identical over 3 fresh loads × 2 shuffled orders.
If not, stop: the card's row is "not reproducible", and that is the finding.

## 7. The space, exported and measured

On the node (München), the spaces calibrated with these models — gutenberg's at
least:

```
docker exec ix-louise-rag-worker-1 python -m tools.cross_vendor_check export gutenberg /tmp/space-gutenberg
# copy /tmp/space-gutenberg (codex.json + its calibration member, ~10 MB) to the VM's /data/space-gutenberg
```

On the VM:

```
$L -v /data/space-gutenberg:/space python -m tools.cross_vendor_check measure /space \
   --url http://localhost:30286 --model vega-qwen3-embedding-4b \
   --twin vega-qwen3-embedding-4b-cpu --twin-texts 64 --twin-limit-s 2400 \
   --out /space/result-t4.json
```

It prints, per role (document, query), the whole set of 512 texts against the
node's reference — worst, median, how many below 0.995 — and the card against
its CPU twin on 64 texts spread over the set (fewer if the limit comes first;
it says how many). The card's vectors come from the gate's own code, so these
are the numbers a node's `hold embedder check` would print.

## 8. Home, and delete the VM

```
scp louise@<ip>:/data/space-gutenberg/result-t4.json .     # and the step 6 outputs
az group delete -n louise-nv --yes --no-wait
```

The printed lines go to the architecture session, which fills the NVIDIA row in
docs/notes/approved-encoders.md and proposes the threshold to the operator.
Nothing measured here is a run of any collection: no vector is written, no
statement kept, nothing published.
