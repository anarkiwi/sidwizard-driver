# sidwizard-driver

Python harness that drives [SID-Wizard](https://csdb.dk/release/?id=258573)
inside [asid-vice](https://github.com/anarkiwi/asid-vice) and captures
per-frame SID register writes from its real 6502 player. Output is a
deduplicated `(frame, reg, value)` CSV — ground truth for verifying
pure-Python SWM players like
[pysidwizard](https://github.com/anarkiwi/pysidwizard).

Built on [vice-driver](https://pypi.org/project/vice-driver/).

## Install

```
pip install sidwizard-driver
```

Requires Docker (or compatible) and the `asid-vice:latest` image — see
[anarkiwi/asid-vice](https://github.com/anarkiwi/asid-vice).

The SID-Wizard editor disk is fetched on demand from CSDB and cached
under `~/.cache/sidwizard-driver/` (override with `--d64 <path>` or
`XDG_CACHE_HOME`). Pre-fetch:

```
python -m sidwizard_driver.fetch
```

## Usage

Boot smoke:

```
python -m sidwizard_driver.smoke
```

Capture a tune to CSV:

```
python -m sidwizard_driver.capture --swm tune.swm --frames 1500 --out tune.csv
```

Dump per-frame player ghost-register state:

```
python -m sidwizard_driver.ghost_dump --swm tune.swm --frames 60 --annotate --out ghost.csv
```

## CSV schema

`capture` emits `frame,reg,value` (integers). Frame 0 is the first
player tick after F1. `reg` is the SID register offset (`0..0x18`).
Consecutive writes of the same value to the same register are
collapsed.

`ghost_dump` emits `frame,addr,value` (with optional `label` column).

## License

Apache-2.0.
