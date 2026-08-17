# Tolerance calibration (F2)

> **These numbers are illustrative, not measured.** The committed
> fixtures are synthetic stand-in backends (same base vectors plus
> backend-flavored numeric noise; an independent draw for the
> different-model lane), generated deterministically by
> `tools/calibrate_space.py --synth` with the recorded seed. They
> exercise every statistic in this harness, but a deployment's real
> tau comes from re-running with `--live` against at least two real
> embed endpoints. What F2 delivers is the harness; what it cannot
> deliver without hardware is the physics.

Reference backend: `reference/cuda/f16` — 512 chunks, dim 256.

## Recommended tolerance

    tau = 0.999987

- placement: worst same-model cross-backend cosine minus a 3-sigma guard (6.50e-06)
- honest miss rate at tau (f0): 0
- false-reject probability for an honest bundle at n=16 (exact lattice, not Wald): 1.6e-08
- detection of 5% corruption at n=16: 0.560 — §6's (1-f)^n budget, unchanged: the per-importer number is deliberately weak and the network argument carries the rest
- margin from tau down to the different-model p99: 0.742

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
+1.0000 |██████████████████████████████████████████████████ | 1024
tau marker: 0.999987
```

## Quantization drift — how far same-weights variants reach down

- `quant/cuda/q8`: worst cosine 1.000000 — INSIDE tau (indistinguishable from honest)
- `quant/cuda/q4`: worst cosine 0.997361 — below tau (distinguishable)

## Different model — the chasm

```
-0.2000 |                                                   | 0
-0.1700 |                                                   | 0
-0.1400 |▍                                                  | 1
-0.1100 |                                                   | 0
-0.0800 |▉                                                  | 2
-0.0500 |█▉                                                 | 4
-0.0200 |██████▋                                            | 14
+0.0100 |█████████████████████▍                             | 45
+0.0400 |██████████████████████████████▍                    | 64
+0.0700 |██████████████████████████████████████████████████ | 105
+0.1000 |█████████████████████████████████████████▍         | 87
+0.1300 |█████████████████████████████████████▌             | 79
+0.1600 |█████████████████████████████                      | 61
+0.1900 |███████████████▋                                   | 33
+0.2200 |████▎                                              | 9
+0.2500 |██▊                                                | 6
+0.2800 |▍                                                  | 1
+0.3100 |▍                                                  | 1
+0.3400 |                                                   | 0
+0.3700 |                                                   | 0
+0.4000 |                                                   | 0
+0.4300 |                                                   | 0
+0.4600 |                                                   | 0
+0.4900 |                                                   | 0
+0.5200 |                                                   | 0
+0.5500 |                                                   | 0
+0.5800 |                                                   | 0
+0.6100 |                                                   | 0
+0.6400 |                                                   | 0
+0.6700 |                                                   | 0
+0.7000 |                                                   | 0
+0.7300 |                                                   | 0
+0.7600 |                                                   | 0
+0.7900 |                                                   | 0
+0.8200 |                                                   | 0
+0.8500 |                                                   | 0
+0.8800 |                                                   | 0
+0.9100 |                                                   | 0
+0.9400 |                                                   | 0
+0.9700 |                                                   | 0
```

p50 0.109, p99 0.258 — nowhere near tau; model substitution is not a marginal call at any plausible tolerance.

## The adversary's perturbation budget

Largest epsilon whose perturbed vectors still pass at tau (p01 of the perturbed cosine above tau):

    epsilon_budget = 2.10e-04

| epsilon | cos p50 | cos p01 | passes at tau |
|---|---|---|---|
| 1.00e-04 | 0.999999 | 0.999998 | yes |
| 1.45e-04 | 0.999997 | 0.999997 | yes |
| 2.10e-04 | 0.999994 | 0.999993 | yes |
| 3.04e-04 | 0.999988 | 0.999986 | no |
| 4.40e-04 | 0.999975 | 0.999971 | no |
| 6.37e-04 | 0.999948 | 0.999938 | no |
| 9.22e-04 | 0.999891 | 0.999868 | no |
| 1.34e-03 | 0.999775 | 0.999725 | no |
| 1.93e-03 | 0.999524 | 0.999412 | no |
| 2.80e-03 | 0.999004 | 0.998794 | no |
| 4.06e-03 | 0.997932 | 0.997493 | no |
| 5.88e-03 | 0.995604 | 0.994738 | no |
| 8.51e-03 | 0.990938 | 0.989095 | no |
| 1.23e-02 | 0.981241 | 0.977444 | no |
| 1.78e-02 | 0.962007 | 0.953814 | no |
| 2.58e-02 | 0.924495 | 0.906309 | no |
| 3.74e-02 | 0.858078 | 0.824658 | no |
| 5.42e-02 | 0.755594 | 0.698376 | no |
| 7.85e-02 | 0.623430 | 0.522377 | no |
| 1.14e-01 | 0.480508 | 0.375939 | no |
| 1.65e-01 | 0.351747 | 0.223136 | no |
| 2.38e-01 | 0.253855 | 0.111909 | no |
| 3.45e-01 | 0.179742 | 0.025678 | no |
| 5.00e-01 | 0.130979 | -0.022579 | no |

## Should the default tau move?

The shipped `tau_default` (0.995) sits BELOW this calibration's recommendation (0.999987). On these fixtures the recommendation is the number to adopt — but per the note above, only a `--live` run against real backends earns the right to change the shipped default. The harness prints the artifact-ready block either way; adopting it is `tools/build_space_artifact.py --calibration`.

## Reproduce

```
python -m tools.calibrate_space --recorded tests/fixtures/calibration \
    --out-json calibration.json --out-report docs/calibration.md
```