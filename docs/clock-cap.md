# The second waste walk: the GPU clock

The [first walk](../README.md) followed the agent's token stream. This one follows the electricity under it.

**Lean calls it overprocessing: doing more work than the customer pays for.** Under load, a DGX Spark's GB10 GPU
runs at about 2,480 MHz. Decode, the part a chat user waits on, is limited by memory bandwidth, not by clock speed, so most
of that clock buys heat instead of tokens. Heat matters here: this setup has hard-powered-off near 96 °C before.

![Speed kept vs GPU power at six clock caps](clock-cap.svg)

## The experiment: one factor, six levels

A process-window study on the live engine. The engine stayed up, and nothing else was changed between levels.

- **Factor:** GPU graphics-clock cap, `nvidia-smi -lgc 0,<MHz>`, both Sparks capped together.
- **Levels:** uncapped → 2300 → 2200 → 2100 → 2000 → 1900 → uncapped again. The repeat checks for drift:
  139.6 W vs 138.6 W, and decode 60.9 vs 61.1 tok/s.
- **Line:** two DGX Sparks, DeepSeek-V4.1-Flash (2.9 bpw) on TensorFold tensor-parallel across both, the same engine
  Hermes Agent talks to all day. Driver 580.178.04.
- **Per level:** four ~70 s reps (after two warm-up reps at the start): one-stream decode (prose and code), four
  streams at once, cold 8K and 32K prefill. 1 Hz telemetry on both nodes: SM clock, GPU-rail watts, GPU temperature,
  throttle bits, hottest ACPI thermal zone.
- **Responses:** speed as % of uncapped, pair GPU-rail watts, temperatures.

## Results

| Cap | Decode, 1 stream | 4 streams | 32K cold prefill (best of 4) | GPU rail, pair | GPU mean n1 / n2 | Hottest ACPI zone, mean n1 / n2 |
|---|---:|---:|---:|---:|---:|---:|
| uncapped (~2,480 MHz) | 61.0 tok/s | 103.3 tok/s | 9.93 s | 139 W | 75 / 77 °C | 86 / 87 °C |
| **2300** | 99.7% | 99.3% | **98.6%** | **101 W** | 69 / 69 | 83 / 82 |
| 2200 | 98.7% | 99.3% | 95.8% | 93 W | 67 / 67 | 82 / 81 |
| 2100 | 97.6% | 100% | 93.7% | 85 W | 66 / 64 | 81 / 79 |
| 2000 | 99.5% | 100% | 91.6% | 78 W | 62 / 65 | 77 / 79 |
| 1900 | 96.6% | 98.9% | 89.2% | 70 W | 61 / 63 | 76 / 76 |

**Decode doesn't care.** It is flat to 2000 MHz within rep-to-rep noise (±1–2%). Only 1900 is a real loss: three clean
reps at 58.9–59.0 tok/s against 61.0. Generating a token means reading the active weights from memory, so the clock waits on
memory bandwidth.

**Prefill pays, roughly in step with the clock.** Reading a long prompt is compute work, so it slows as the clock comes
down.

**The knee is the first step.** Price each step in watts saved per point of prefill given up:

| Step | Watts saved | Prefill given up | Watts per point |
|---|---:|---:|---:|
| uncapped → 2300 | 38.4 W | 1.4 pts | **27** |
| 2300 → 2200 | 7.7 W | 2.8 pts | 2.7 |
| 2200 → 2100 | 8.5 W | 2.1 pts | 4.1 |
| 2100 → 2000 | 6.9 W | 2.1 pts | 3.4 |
| 2000 → 1900 | 7.5 W | 2.5 pts | 3.1 |

The top ~200 MHz is almost pure waste: 28% of the GPU-rail power for 1.4% of prefill. Below 2300, each step is a seven to
ten times worse trade. **2300 MHz** captures 38 of the 46 W that 2200 saves, at a third of 2200's prefill cost. **2000** is
the heat setting for a hot day: decode unchanged, prefill −8%, rail −44%, GPU −12 °C.

**It agrees with the community number.** [Mia AI Lab](https://x.com/MiaAI_lab/status/2115804348068278780) shared Lasse's
sweep of two Sparks running Mia's DeepSeek-V4.1-Flash recipe and recommended 2200 MHz for every Spark. At 2200, that sweep measured 95.3% prefill and 132 → 89 W, compared with 95.8% and 139 → 93 W here. Two
different pairs give the same curve. This pair's own data puts the knee one step higher.

## Confirmation run on the line

2300 MHz has been live on both nodes since **2026-10-29 21:00 CDT** as a boot-time systemd unit. The confirmation soak
ran right after: 28 minutes, 25 reps, 22 clean.

| | Uncapped (sweep) | 2300, soak |
|---|---:|---:|
| Loaded SM clock n1 / n2 | 2,470 / 2,496 MHz | 2,268 / 2,275 MHz |
| Throttle bits set | none | none |
| Decode prose / code / 4 streams | 61.0 / 91.3 / 103.3 tok/s | 61.8 / 92.5 / 105.1 |
| 32K cold prefill, median / best | 9.94 / 9.93 s | 10.18 / 10.02 s (97.6% / 99.1%) |
| GPU rail, pair | 139 W | 105 W |
| GPU mean / peak n1 / n2 | 75 / 77 · 81 / 82 °C | 70 / 71 · 76 / 78 °C |
| Hottest ACPI zone, mean n1 / n2 | 86 / 87 °C | 84 / 83 °C |
| **Hottest ACPI zone, max n1 / n2** | **89.1 / 91.6 °C** | **91.0 / 92.7 °C** |

Temperatures were flat: GPU means in the first and last ten minutes are within 0.7 °C. The next morning, after 13 hours, both nodes still held
2,275 MHz.

## What the countermeasure did not fix

Look at the last row. The cap lowered **average** heat: GPU −5 to −6 °C, hottest ACPI zone −2 to −4 °C. It did not
lower the **peaks**. The hottest zone still spiked to 92.7 °C, no lower than uncapped. The GPU clock does not drive those
spikes, so the cap buys no margin against a 96 °C power-off.

In quality terms, it was a good countermeasure for a contributing cause and did nothing to the root cause. Every
thermal zone reports as a generic `acpitz`, so which part of the chip spikes is still unknown. A zone that moves
less than the GPU fits CPU and SoC heat from the engine's host threads, but that is a lead, not a finding. The next step is
to find that sensor.

## Limits

- **Rail watts are not wall watts.** The GPU rail is only part of what a Spark draws from the outlet. The electricity
  bill does not fall 28%.
- **One evening, indoors, late October,** n = 4 per level. A hot room shifts the whole curve.
- **Descending order.** Each capped level may still carry heat from the level before it, so the capped temperatures
  read high, not low. Heat phases skip their first 60 s.
- **Prefill uses the best rep, not the median.** Sporadic 1–1.8 s stalls hit every level, uncapped included (one clean
  uncapped rep took 11.72 s), so medians are noise. The best-of-4 curve is monotonic. Cold 8K prefill is bimodal
  (≈3.0 or ≈5.5 s) at every level and isn't used.
- **About one rep in four overlapped live agent traffic** and is left out of the speed numbers.
- **This pair was not thermally braking.** No throttle bit was set even uncapped. On a box that already throttles,
  a cap can *gain* prefill back. [One published TP2 run](https://github.com/agjs/gb10-clock-cap) at 2200 measured +3.9% prefill on a hotter pair.

## Do it on your own Spark

```bash
sudo nvidia-smi -lgc 0,2300      # takes effect immediately, no reboot; lost at reboot
nvidia-smi --query-gpu=clocks.sm,power.draw,temperature.gpu --format=csv
sudo nvidia-smi -rgc             # undo
```

To keep the cap across reboots, run the same line from a oneshot systemd unit. Don't use `-rgc` as the unit's
`ExecStop`, because restarting the unit would then briefly unlock the clock. Measure your own pair before you pick a
number. The method is above, and these raw results are the reference.

## Data

[`examples/clock-2026-10-29/`](../examples/clock-2026-10-29/) holds the sweep and the soak: one line per rep
(`reps.jsonl`), the phase timeline, and per-level summaries. Running `chart.py` there redraws this chart from them. The
1 Hz telemetry (≈700 KB) stays local.
