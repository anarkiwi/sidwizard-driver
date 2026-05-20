# Shared-infra candidates (sidwizard-driver vs defmon-driver)

Living index of modules vendored from `defmon-driver` and what kind of
sharing they would justify if both projects were refactored onto a
common `asid-vice-driver` base. Updated as each module is touched.

Categories:
- **extract** — vendored verbatim; zero edits; an upstream extraction would
  literally make both repos thinner.
- **split** — mostly editor-agnostic, but one or two methods need an
  editor-specific shim. Right shape is a base class in the shared package
  with per-editor subclasses in each driver.
- **rewrite** — copy-paste would mislead; functionally similar but tightly
  coupled to the editor's data model or memory layout. Each driver owns
  its own.
- **skip (v0)** — not vendored yet; revisit when the corresponding use
  case appears.

| defmon-driver module | LOC | Category | Notes |
|---|---|---|---|
| `binmon.py` | 952 | **extract** | Pure asid-vice wire protocol. Zero defMON-specific content. Vendored verbatim. |
| `vice_docker.py` | 197 | **extract** | Container lifecycle + `sounddev=dump` plumbing. Zero editor coupling. Vendored verbatim. |
| `keys.py` | 277 | **extract** | C64 key matrix + ASCII→chord. Mirrors `mon_keymatrix.c`. Vendored verbatim. |
| `keycode_table.py` | 282 | **skip (v0)** | Symbolic chord-name calibration. Useful once the driver grows beyond hard-coded F1; not needed for the capture loop. |
| `bootstrap_keycodes.py` | 88 | **skip (v0)** | Imports `Defmon`. To share, would need to be parameterised on an editor-shaped trait (open menu / read screen). |
| `screen.py` | 174 | **skip (v0)** | SCREEN_GET parsing + screencode→ASCII. Editor-agnostic in principle, but the capture loop uses an IRQ checkpoint for readiness instead. Likely **extract** when the editor automation phase lands. |
| `keyhandler.py` | 718 | **split** | Direct-call key injection bypassing CIA debounce. Mechanism is generic; the handler entry-point address inside ROM is per-editor. Probably a base class + subclass each provides its `KEYHANDLER_ENTRY` constant. |
| `coverage.py` | 370 | **extract (likely)** | CHECK_STORE/EXEC harness over the binmon Coverage API. Editor-agnostic on inspection. Confirm when ported. |
| `defmon.py` | 1500 | **rewrite** | Tightly coupled to defMON's screen layout and keyboard map. Sidwizard equivalent (`sidwizard.py`) is the analogue. |
| `field_setter.py` | 875 | **rewrite** | defMON UI modal model. Sidwizard's UI is different (pattern / sequence / instrument editors); the field-setter abstraction itself may extract once a second implementation exists. |
| `sidtab.py`, `calibrate_sidtab.py`, `tune_manifest.py`, `tune_navigation.py` | — | **rewrite or omit** | defMON-data-model-specific. Sidwizard's data model is covered by `pysidwizard`'s reader/writer. |

## Proposed shared package layout (if extracted later)

```
asid_vice_driver/
    binmon.py          # extract
    vice_docker.py     # extract
    keys.py            # extract
    screen.py          # extract (deferred — first user needed)
    keycode_table.py   # extract
    coverage.py        # extract (confirm on second use)
    keyhandler/
        __init__.py    # base class with the direct-call mechanism
        # subclasses register an editor-specific entry-point address
```

Then each driver depends on `asid-vice-driver` and ships only its
editor-specific layer (`defmon.py`, `sidwizard.py`, field setters,
calibration JSON, etc.).

## Recommendation

Vendor first; revisit extraction once at least two of `keyhandler`,
`screen`, `coverage` are in use in both drivers. Premature extraction
would block on a defmon-driver refactor that doesn't pay for itself
until the sidwizard-driver editor phase exists.
