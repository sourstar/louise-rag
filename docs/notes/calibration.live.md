# Tolerance calibration (F2)

Reference backend: `http://<node>:30436` — 512 chunks, dim 4096.

## Recommended tolerance

    tau = 0.999994

- placement: worst same-model cross-backend cosine minus a 3-sigma guard (7.79e-07)
- honest miss rate at tau (f0): 0
- false-reject probability for an honest bundle at n=16 (exact lattice, not Wald): 1.6e-08
- detection of 5% corruption at n=16: 0.560 — §6's (1-f)^n budget, unchanged: the per-importer number is deliberately weak and the network argument carries the rest
- margin from tau down to the different-model p99: 0.500

## Same model, different backend — the floor tau sits above

```
+0.9995 |                                                   | 0
+0.9995 |                                                   | 0
+0.9995 |                                                   | 0
+0.9995 |                                                   | 0
+0.9995 |                                                   | 0
+0.9996 |                                                   | 0
+0.9996 |                                                   | 0
+0.9996 |                                                   | 0
+0.9996 |                                                   | 0
+0.9996 |                                                   | 0
+0.9996 |                                                   | 0
+0.9996 |                                                   | 0
+0.9996 |                                                   | 0
+0.9997 |                                                   | 0
+0.9997 |                                                   | 0
+0.9997 |                                                   | 0
+0.9997 |                                                   | 0
+0.9997 |                                                   | 0
+0.9997 |                                                   | 0
+0.9997 |                                                   | 0
+0.9997 |                                                   | 0
+0.9998 |                                                   | 0
+0.9998 |                                                   | 0
+0.9998 |                                                   | 0
+0.9998 |                                                   | 0
+0.9998 |                                                   | 0
+0.9998 |                                                   | 0
+0.9998 |                                                   | 0
+0.9998 |                                                   | 0
+0.9999 |                                                   | 0
+0.9999 |                                                   | 0
+0.9999 |                                                   | 0
+0.9999 |                                                   | 0
+0.9999 |                                                   | 0
+0.9999 |                                                   | 0
+0.9999 |                                                   | 0
+0.9999 |                                                   | 0
+1.0000 |                                                   | 0
+1.0000 |                                                   | 0
+1.0000 |██████████████████████████████████████████████████ | 512
tau marker: 0.999994
```

## Quantization drift — how far same-weights variants reach down


## Different model — the chasm

_No different-model lane in this recording (a live run measures same-weights backends); the chasm reference in the calibration block is the labeled placeholder._


## The adversary's perturbation budget

Largest epsilon whose perturbed vectors still pass at tau (p01 of the perturbed cosine above tau):

    epsilon_budget = 0.00e+00

| epsilon | cos p50 | cos p01 | passes at tau |
|---|---|---|---|
| 1.00e-04 | 0.999980 | 0.999979 | no |
| 1.45e-04 | 0.999957 | 0.999955 | no |
| 2.10e-04 | 0.999910 | 0.999906 | no |
| 3.04e-04 | 0.999812 | 0.999803 | no |
| 4.40e-04 | 0.999605 | 0.999583 | no |
| 6.37e-04 | 0.999169 | 0.999127 | no |
| 9.22e-04 | 0.998262 | 0.998178 | no |
| 1.34e-03 | 0.996360 | 0.996190 | no |
| 1.93e-03 | 0.992420 | 0.992004 | no |
| 2.80e-03 | 0.984315 | 0.983418 | no |
| 4.06e-03 | 0.967929 | 0.966274 | no |
| 5.88e-03 | 0.935969 | 0.932982 | no |
| 8.51e-03 | 0.878233 | 0.871624 | no |
| 1.23e-02 | 0.785002 | 0.774580 | no |
| 1.78e-02 | 0.657855 | 0.639038 | no |
| 2.58e-02 | 0.516525 | 0.489784 | no |
| 3.74e-02 | 0.384773 | 0.356711 | no |
| 5.42e-02 | 0.277805 | 0.244984 | no |
| 7.85e-02 | 0.195466 | 0.155256 | no |
| 1.14e-01 | 0.135291 | 0.100429 | no |
| 1.65e-01 | 0.093402 | 0.060991 | no |
| 2.38e-01 | 0.064718 | 0.027191 | no |
| 3.45e-01 | 0.045049 | 0.009780 | no |
| 5.00e-01 | 0.031739 | -0.006776 | no |

## Should the default tau move?

The shipped `tau_default` (0.995) sits BELOW this calibration's recommendation (0.999994). On these fixtures the recommendation is the number to adopt — but per the note above, only a `--live` run against real backends earns the right to change the shipped default. The harness prints the artifact-ready block either way; adopting it is `tools/build_space_artifact.py --calibration`.

## Reproduce

```
python -m tools.calibrate_space --recorded tests/fixtures/calibration \
    --out-json calibration.json --out-report docs/calibration.md
```