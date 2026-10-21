# cli-anything-rea

Harness for REA (`~/codes/rea`, morluto/rea) on this Mac. Design notes: `../../REA.md`.

## Install

```bash
uv tool install --editable tools/cli-anything-rea
```

Needs `~/codes/rea` (built) and `~/codes/rea-deps/rea.env`. Override with `REA_SRC` / `REA_DEPS`.

## Commands

| Command | Answers |
|---|---|
| `src head` | which commit the clone is at; whether `references/main-<sha7>` matches |
| `src layers` | the layers and which reference doc reads each |
| `src find SYMBOL [--path P] [--ref R]` | where it is declared |
| `src show PATH:LINE [-n N]` | the code there |
| `src grep PATTERN [-F] [--path P]` | PCRE grep at a ref |
| `src env-vars` | every `REA_*` name in non-test source |
| `tools list [--destructive] [--grep S]` | the 210-tool table with side-effect flags |
| `tools verify` | live `tools/list` equals the table (names + annotations) |
| `deps status` / `deps env` | every dependency executes; what rea.env sets |
| `deps unkill` / `deps build` | Huorong 137 workaround; ci → unkill → build |
| `run doctor` / `run providers` | `rea doctor` split into engines vs agent registrations |
| `run decompile BIN PROC [--provider ghidra\|kuna]` | real decompile, summary + saved Evidence |
| `run analyze-js DIR\|ASAR [--record-integrity]` | static Electron reconstruction summary |

## Tests

```bash
cd tools/cli-anything-rea
CLI_ANYTHING_FORCE_INSTALLED=1 "$(uv tool dir)/cli-anything-rea/bin/python" -m pytest cli_anything/rea/tests -v
```

`test_e2e.py` runs REA for real and fails (does not skip) when it cannot.
