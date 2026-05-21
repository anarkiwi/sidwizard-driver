# AGENTS.md — sidwizard-driver (proposal)

Operating notes for the next agent picking this up. This repo is empty
at the time of writing (only `LICENSE`); everything below is the spec
the next agent should implement.

## What this repo is (proposed)

A Python automation framework for driving the C64 build of
**[SID-Wizard](https://csdb.dk/release/?id=258573)** (Hermit / Mihaly
Horvath) running inside
**[asid-vice](https://github.com/anarkiwi/asid-vice)** — the same
extended VICE C64 emulator that backs
**[defmon-driver](https://github.com/anarkiwi/defmon-driver)**. The
primary deliverable is a CLI that:

1. boots SID-Wizard from its distributed `.d64`,
2. side-loads a `.swm` module under test,
3. starts playback (F1),
4. captures every SID register write `($D400-$D418)` per PAL frame for
   `N` frames, and
5. emits a deduplicated `(frame, reg, value)` CSV in the **same
   schema** as `pysidwizard.player.render_wav`'s sibling `.csv`.

That CSV is the ground truth needed to verify
**[pysidwizard](file:///scratch/anarkiwi/pysidwizard)**'s
`SWMPlayer` (the per-frame SWM player at
`pysidwizard/src/pysidwizard/player.py`). The scope doc inside that
repo (search for "Verification strategy") explicitly nominates this
driver as the missing piece.

Secondary goal: provide editor automation (load / play / edit
pattern + instrument fields / save) for SID-Wizard the same way
defmon-driver does for defMON, so other tooling can drive it
headlessly.

## Why this exists

`pysidwizard.player.SWMPlayer` is a from-scratch Python reimplementation
of SID-Wizard's 3300-line 6502 player driver
(`/tmp/sidwizard/SID-Wizard-1.94/native/sources/include/player.asm`).
At the time of writing it covers Tier-0 features only (sequence /
pattern / WF-PW-Filter tables / hard-restart / main-vol-and-tempo FX)
and explicitly skips vibrato, portamento, chord tables, tempo
programs, and most small/big FX. To close that gap responsibly the
implementer needs per-frame SID-register diffs against the real
player. That's what this driver produces.

Without ground-truth captures every "fix" is guesswork. With them, the
work in `pysidwizard`'s Tier-1 / Tier-2 / Tier-3 scope (see the same
scope doc) becomes a tight loop: implement → diff → next failing
frame → fix.

## Inputs you can rely on

| Path | What it is |
|---|---|
| `/scratch/anarkiwi/sidwizard-driver/` | This repo — empty except `LICENSE`. |
| `/scratch/anarkiwi/defmon-driver/` | Reference implementation. **Read first.** Same emulator stack, same binmon protocol. |
| `/scratch/anarkiwi/pysidwizard/` | The pure-Python SWM reader / writer / player to verify. |
| `/scratch/anarkiwi/pysidwizard/tests/data/*.swm` | Four real SWM samples (`flashitback`, `bronkosaurus`, `euphoria`, `rain8580`). |
| `/scratch/anarkiwi/pysidwizard/src/pysidwizard/player.py` | The player under test; its `render_wav` writes a CSV in the schema this driver must match. |
| `/tmp/sidwizard/SID-Wizard-1.94/` | Extracted upstream source — `disk1.d64`, full 6502 source under `native/sources/`. May need re-downloading after a reboot (see below). |
| `/tmp/sidwizard/SID-Wizard-1.94/SID-Wizard-1.94-disk1.d64` | The `.d64` to autostart. |
| `https://csdb.dk/getinternalfile.php/276275/SID-Wizard-1.94-with-sources.tar.gz` | Source archive if `/tmp` is gone. ~8.5 MB. |
| `https://github.com/anarkiwi/asid-vice` | The VICE fork that exposes the binary monitor extensions this driver relies on (KEYMATRIX_*, SCREEN_GET, DRIVE_ATTACH). |

## Code-reuse policy: depend on `vice-driver` (upstream)

The editor-agnostic asid-vice client has been extracted into the
**[vice-driver](https://pypi.org/project/vice-driver/)** PyPI package.
This repo depends on it (see ``pyproject.toml``) and imports
``BinMon``, ``ViceContainer``, ``KEY`` / ``lookup`` / ``text_to_chords``,
``ScreenSnapshot`` / ``parse_screen_response`` directly from
``vice_driver``. ``vice_driver.coverage`` (CHECK_STORE/EXEC harness)
and ``vice_driver.expect`` (post-action assertion helpers) are also
available when the editor-automation phase wants them.

Only SID-Wizard- and SWM-specific code lives here:
``sidwizard_driver.sidwizard`` (boot flow, TUNEHEADER discovery,
disk-menu nav, F1 play), ``sidwizard_driver.d64`` (single-PRG disk
writer used to deliver SWMs through the editor's own loader), and
``sidwizard_driver.dump`` (decoder for VICE's ``sounddev=dump``
trace). See ``SHARED_INFRA.md`` for the up-to-date split.

## Phase 0 — bootstrap (half a day)

Goal: prove the toolchain works end-to-end before writing any
SID-Wizard-aware code.

1. `pip install vice-driver>=0.1.0` (already declared in
   ``pyproject.toml``). No vendoring — this repo imports the protocol
   client, container lifecycle, key matrix, and screen scrape directly
   from ``vice_driver``.
2. Add a `sidwizard_driver/smoke.py` that:
   - spins up a `ViceContainer` autostarting `disk1.d64`,
   - connects `BinMon`, calls `bm.exit()` to resume the CPU,
   - waits ~30 s, calls `bm.screen_get()`, and prints the screen text.
   - **Expected output**: the SID-Wizard main UI (a tracker grid).
4. Commit a `tests/unit/` mirror of defmon-driver's offline tests so
   the binmon protocol layer is regression-tested.

**Done when**: `python -m sidwizard_driver.smoke /path/to/disk1.d64`
prints the SID-Wizard main screen.

## Phase 1 — load .swm + play (1 day)

Goal: replace defmon-driver's `defmon.py` with a SID-Wizard equivalent.

1. Read `/tmp/sidwizard/SID-Wizard-1.94/native/sources/include/keyhandler.inc` to enumerate the key handlers. The key set you need first:
   - **F1** = reset + play whole tune from beginning (`f1er` at line 2099)
   - **F3** = play current pattern from beginning (`f3er`)
   - **F4** = stop / pause (`f4er` / `runstop`)
   - **F7** = enter disk menu
   - The text-input handlers for typing a filename
2. Write `sidwizard_driver/sidwizard.py` exposing a `Sidwizard` class
   (analogous to `Defmon`) with at minimum:
   - `wait_for_loaded(timeout)` — poll the screen until the
     SID-Wizard splash clears.
   - `open_disk_menu()` / `select_file(name)` / `close_disk_menu()` —
     load a `.swm` from drive 8.
   - `play_from_start()` (F1) / `stop()` (F4).
   - `screen()` — return a `ScreenSnapshot`.
3. Add an integration smoke
   (`sidwizard_driver/smoke_play.py path/to/disk1.d64
   path/to/tune.swm`) that loads + plays one of pysidwizard's sample
   `.swm` files. **Expected behaviour**: the screen scrolls / cursor
   advances as the tune plays.

**Done when**: an `.swm` from `pysidwizard/tests/data/` loads and the
screen confirms playback is in progress.

## Phase 2 — capture SID writes (1 day)

Goal: produce the `(frame, reg, value)` CSV that pysidwizard needs.

There are two viable capture paths; pick **A** first, fall back to **B**
if it doesn't carry per-frame info:

**A. VICE `sounddev=dump`.** `ViceContainer` already plumbs through
`sounddev="dump"` + `sounddump_path`. VICE writes a binary record per
SID write into that file (`src/sounddrv/sounddump.c` in VICE — verify
the format against your installed VICE version; historically: one
byte per cycle delta + register + value). A post-processor needs to:

1. Mount a host-side dump file via a `DiskMount`.
2. Stop the container, read the file, decode it into
   `(cycle, reg, value)` records.
3. Quantise cycles into PAL frames (`19656` cycles/frame on
   PAL — same constant pysidwizard's player uses).
4. Optionally deduplicate consecutive writes of the same value to the
   same register, matching pysidwizard's CSV semantics.

**B. CHECK_STORE checkpoints on `$D400-$D418`.** If `sounddev=dump`
turns out to be missing frame info, install 25 `CHECK_STORE`
checkpoints (one per SID register) plus a `CHECK_EXEC` on
SID-Wizard's IRQ entry to mark frame boundaries; harvest hits via the
binmon `cpuhistory` ring. `defmon_driver/coverage.py` shows the
pattern (but for `CHECK_EXEC`).

**Done when**: `python -m sidwizard_driver.capture --d64 disk1.d64
--swm flashitback.swm --frames 1500 --out flashitback.reference.csv`
emits a CSV that `python -c 'import csv; print(sum(1 for _ in
csv.reader(open(...)))-1)'` says has > 1000 rows.

## Phase 3 — validate pysidwizard (half a day)

Goal: produce the first useful diff against pysidwizard's player.

1. Add `sidwizard_driver/diff_pysidwizard.py` that takes two CSVs
   (this driver's reference + a pysidwizard player CSV) and prints
   `(frame, reg, pysidwizard_val, sidwizard_val)` for every
   diverging frame.
2. Pick `flashitback.swm` (the smallest and simplest of the four
   shipped samples) and commit the captured CSV as
   `sidwizard_driver/fixtures/flashitback.reference.csv`.
3. Add a CI test in pysidwizard that runs its player against the same
   `.swm`, compares the first N frames against the committed
   reference, and asserts ≥ X% per-class match. Start with low
   thresholds (the player is intentionally incomplete) and ratchet
   them up as pysidwizard's player gains features.

**Done when**: pysidwizard CI fails on a SID-Wizard player change
that drifts the trace from the captured reference.

## Phase 4 — editor automation (optional, multi-day)

Goal: parity with defmon-driver's editor side so other tooling can
edit SID-Wizard tunes headlessly.

Possible workstreams, in priority order:

1. **Field-setter**. SID-Wizard's UI is modal (pattern editor vs.
   sequence editor vs. instrument editor). A field setter that
   tab/cursors to a named field and types hex is the building block.
2. **Coverage harness**. `defmon_driver.coverage.Coverage` is reusable
   verbatim against SID-Wizard's player band — useful for verifying
   that a given `.swm` actually exercises the bits of player.asm we
   think it does.
3. **Disk save / overwrite**. Mirror of defmon-driver's
   `disk_save_new` / `disk_save_overwrite`. Important if anyone wants
   to use this driver to edit + save tunes.
4. **A pytest plugin** exposing a session-scoped `vice_container` and
   a function-scoped `sidwizard` fixture (modelled after
   defmon-driver's `FUTURE.md` "Pytest plugin / fixture for a shared
   VICE container").

## Non-goals

* **Replacing pysidwizard's player.** This driver is verification
  infrastructure, not a player. The Python player stays in
  pysidwizard; this driver only feeds it ground-truth diffs.
* **Multi-SID (2SID/3SID/4SID) support in v0.** Single-SID is enough
  to verify pysidwizard's Tier-1 features. `ViceContainer` already
  accepts `sid_extras=` so the wiring is there when needed.
* **Bundling SID-Wizard itself.** Same policy defmon-driver has: the
  user provides the `.d64` themselves; the package ships only the
  automation harness.
* **A SID-Wizard data-model layer.** pysidwizard already has a
  byte-exact `.swm` reader/writer with structured `Row` /
  `Instrument` / `SequenceCommand` types — use that.

## Status (after v0.2 — path B + vice-driver migration)

* Depends on the upstream **vice-driver** package for the asid-vice
  protocol client, container lifecycle, key matrix, and screen scrape.
  No more vendored copies of those modules here.
* `sidwizard_driver/sidwizard.py` — `Sidwizard` class:
  ``wait_for_startup_menu`` → ``dismiss_startup_menu`` →
  ``wait_for_editor`` (the editor doesn't hook ``$0314/$0315``; the
  alive signal is the loadtun signature scan returning a SWM1-pointing
  address); ``load_swm_via_menu`` drives SHIFT+F7 → CRSRDOWN → RETURN
  → typed filename → RETURN; ``play`` taps F1.
* `sidwizard_driver/d64.py` — single-PRG ``.d64`` writer used to deliver
  the SWM through SID-Wizard's own loadtun + depackt path.
* `sidwizard_driver/dump.py` — decoder for VICE's ``sounddev=dump``
  text trace (format confirmed against
  ``src/arch/shared/sounddrv/sounddump.c`` in asid-vice — see Open
  Question 2 below).
* `sidwizard_driver/smoke.py`, `capture.py` — CLI entry points.
* `sidwizard_driver/fixtures/flashitback.reference.csv` — 49,667-row
  reference capture from the live editor for pysidwizard's diff harness.
* 36 offline unit tests covering the dump decoder (incl. truncated
  trailing line), d64 invariants, and the disambiguating TUNEHEADER
  signature scan.

The pieces from "Phase 2 capture" wire together and produce
real-but-not-yet-frame-aligned traces (cycle origin = VICE boot, not
F1 press — that's the recommended next step).

## Open questions

The next agent should pin these down before producing a reference CSV
the pysidwizard player can diff against:

1. **[CLOSED] Where does SID-Wizard's IRQ entry live in RAM?** The
   `Sidwizard.wait_for_idle` polls the `$0314/$0315` IRQ vector and
   declares the editor "idle" once it stops pointing at the KERNAL
   default `$EA31` and stays put for two consecutive polls. No
   editor-internal address needed for the boot signal.
2. **[CLOSED] `sounddev=dump` record format.** Confirmed against
   `src/arch/shared/sounddrv/sounddump.c` in
   `/scratch/anarkiwi/asid-vice`. The active path is `dump_dump2`,
   which writes `"%d %d %d %d %d %d\n"` =
   `<cycle_delta> <irq_delta> <nmi_delta> <chipno> <addr> <byte>` per
   write. The 5-bit register offset (`addr & 0x1f`) is what reaches
   the dump driver — `sound_store` is fed by `sid_store_chip` which
   masks the address. **Caveat:** the dump is only fed for software
   SID engines (FastSID/ReSID); hardware SID variants bypass
   `sound_store`. asid-vice in the supplied Docker image defaults to
   ReSID, so this is fine.
3. **[OPEN — BLOCKER] The side-load gap.** AGENTS.md originally
   proposed writing the `.swm` to `$1FF8` to bypass disk-menu
   automation. That's wrong on two counts:

   * SID-Wizard's `loadtun` (`native/sources/include/menu.inc` near
     line 175) calls `KERNAL.LOAD` with the target address
     `TUNEHEADER`, a **build-time symbol** whose runtime value is
     not constant across editor variants (sidwiz / sidwiz2 / 3 / 4).
     The `$1FF8` in pysidwizard's `DEFAULT_LOAD_ADDRESS` is only the
     load address baked into PRG headers when SID-Wizard **saves**
     a `.swm` — the loader forces it back to `TUNEHEADER` on read.
   * The on-disk `.swm` form is **packed**. After `KERNAL.LOAD`,
     `loadtun` stores the last-loaded address into zero page
     `compzptr` and calls `depackt` to expand the data in place at
     `TUNEHEADER`. A bare `mem_set` to `TUNEHEADER` leaves the data
     in packed form; the player will read garbage.

   `Sidwizard.discover_tuneheader` solves the first half (it pattern-
   scans editor RAM for the `loadtun` immediates — see
   `tests/unit/test_sidwizard.py` for the offline coverage). The
   second half — running `depackt` and the post-load init steps
   (`dispaut`, subtune reset) — is unimplemented.

   Two viable paths for the next agent to pick:

   * **A. Drive depackt directly.** After `mem_set`, set zero page
     `$AE/$AF` (or whatever `compzptr` resolves to — discover the
     same way as TUNEHEADER), then `bm.registers_set({3: depackt_addr})`
     + `bm.run_until_pc(post_depackt_rts)`. Repeat for `dispaut` and
     subtune reset. Discovery work needs new signature scans for
     each routine — feasible but ~half a day.
   * **B. Drive the disk menu.** Pre-make a `.d64` containing the
     `.swm` (either via host-side `c1541` or a small pure-Python
     d64 writer — ~80 LOC for a single PRG entry), `attach_drive` it
     as drive 8 (replacing the editor disk after boot), then drive
     the editor's keyhandler to enter the file menu and `loadtun`
     does the depack for us. Closer to AGENTS.md's original Phase 1
     plan; needs `keyhandler.py` and `screen.py` ported in. ~1 day.

   Recommendation: **B**. It uses the editor's own (correct) load
   path so the trace we capture is exactly what a human user would
   produce, and it forces us to port `keyhandler.py` / `screen.py`
   which we're going to need anyway for the editor-automation phase.
4. **Does SID-Wizard's framespeed > 1 affect frame numbering?** Still
   open. The `dump.py` quantiser takes `cycles_per_frame` so a caller
   can pass `19656 // frame_speed` once the `.swm` header is parsed
   (pysidwizard's reader can extract `frame_speed` cheaply — wire it
   into `capture.py` once side-load works).
5. **Stereo file (`.sws`) support.** Out of scope for v0, but
   pysidwizard's reader recognises `SWMS` magic — keep that door open.

## "Prepare to clear" checklist

This proposal is the durable artefact. After context clears, a fresh
agent should be able to act on it with only the following extra
context retrieval:

* `ls /scratch/anarkiwi/{defmon-driver,pysidwizard,sidwizard-driver}` —
  prove the three repos still exist on this machine.
* `ls /tmp/sidwizard/SID-Wizard-1.94/` — confirm the SID-Wizard source
  tree is still extracted; re-download from CSDB if not (URL above).
* `python -c "import pyresidfp; import pysidwizard; print('ok')"` —
  prove the Python deps the verification end of the loop needs are
  installed.

If any of those fail the proposal still applies; the next agent just
needs to re-fetch the missing inputs.

## Repository conventions

When you start writing code, mirror defmon-driver's house style:

* `requires-python = ">=3.10"`, **zero runtime deps** (stdlib only).
* `pyproject.toml` with the same `[tool.ruff]` and `[tool.pytest]`
  config defmon-driver uses.
* Unit tests live under `tests/unit/` and must run **offline** (no
  Docker). Anything that needs a real container goes under
  `tests/integration/` or as a `smoke_*.py` CLI module.
* Module docstrings start with one summary line, then a "What this
  models / Scope / Not modelled" block — mirrors the style in
  `pysidwizard/src/pysidwizard/player.py` and
  `defmon_driver/binmon.py`.
* Commit messages are imperative and explain the *why*, not the *what*.
