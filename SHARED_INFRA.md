# Shared-infra status

The extraction recommended by the early-prototype edition of this file
has happened: the editor-agnostic asid-vice client now lives in the
upstream **[vice-driver](https://pypi.org/project/vice-driver/)**
package (v0.1.0). This repo depends on it via ``pyproject.toml`` and
no longer carries vendored copies of the protocol modules.

## What moved upstream (re-imported from `vice_driver`)

| Module | Previous local path | Upstream path |
|---|---|---|
| Binmon wire protocol | `sidwizard_driver/binmon.py` | `vice_driver.binmon` |
| Container lifecycle  | `sidwizard_driver/vice_docker.py` | `vice_driver.vice_docker` |
| C64 key matrix + chord typing | `sidwizard_driver/keys.py` | `vice_driver.keys` |
| `SCREEN_GET` parsing | `sidwizard_driver/screen.py` | `vice_driver.screen` |

Upstream also ships ``vice_driver.coverage`` (CHECK_STORE/EXEC harness)
and ``vice_driver.expect`` (post-action assertion helpers); neither is
needed by sidwizard-driver yet but they're available the moment the
editor-automation phase wants them.

## What stays SID-Wizard-/SWM-specific (local)

| Module | Rationale |
|---|---|
| `sidwizard_driver/sidwizard.py` | SID-Wizard-specific boot flow (startup-menu dismissal), TUNEHEADER discovery (SWM1-magic disambiguation), disk-menu navigation (SHIFT+F7 → CRSRDOWN → RETURN → type → RETURN), F1 play. Tightly coupled to SID-Wizard 1.94's UI. |
| `sidwizard_driver/d64.py` | Single-PRG ``.d64`` writer used to deliver SWM modules through SID-Wizard's own loader. Generic enough to extract to `vice_driver` if a second driver ever needs to synthesise disks (defmon-driver has a similar need for tune-import smokes), but only one user today. |
| `sidwizard_driver/dump.py` | Decoder for VICE's ``sounddev=dump`` text trace. Also generic; same "extract on second user" rule applies. |
| `sidwizard_driver/capture.py`, `smoke.py` | CLIs that compose the above. |

## What's NOT migrated yet

`defmon-driver`'s editor-specific modules (`defmon.py`, `field_setter.py`,
`sidtab.py`, `calibrate_sidtab.py`, `tune_manifest.py`, `tune_navigation.py`,
`keyhandler.py`, `bootstrap_keycodes.py`, `keycode_table.py`) — these
stay defmon-driver-local because they encode defMON's data model and
UI; they don't share with SID-Wizard's. If a second user of
``keyhandler.py``'s direct-call mechanism ever appears in this repo
(needed when SID-Wizard automation grows past ~10 keypresses/sec) we'd
revisit the split — at that point ``vice_driver`` would gain a base
class with the dispatch machinery and the per-editor entry-point
address would stay in each driver.
