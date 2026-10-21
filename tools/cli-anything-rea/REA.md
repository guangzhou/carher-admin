# REA agent harness

## Backend analysis

REA (`morluto/rea`, npm `rea-agents`, MIT, TypeScript/Node) is itself a CLI + MCP server for reverse
engineering: 210 MCP tools over Ghidra/Hopper/IDA/Kuna and ~20 static readers (JS/Electron, .NET,
Go, Rust, IL2CPP, WASM, APK, ...). It already has an agent-facing CLI, so this harness does not wrap
REA's analysis surface. It adds what REA's own CLI cannot answer about REA:

| Question | Real backend | Command |
|---|---|---|
| what does the code at commit X say | `git -C ~/codes/rea show/grep <ref>` (no checkout, PCRE) | `src` |
| which tools exist and which are destructive | `src/contracts/toolEffects.ts` **and** a live `rea mcp` `tools/list` | `tools` |
| is every dependency installed and executing here | each tool's version command with `rea.env` applied | `deps` |
| does it actually work on this Mac | `node scripts/rea.mjs …` on a sample we compiled with a known answer | `run` |

Nothing is mirrored: no copied source, no cached catalog. `rea.env` is the single source of provider
paths; the harness parses it.

## Rulers

- "the catalog": runtime `tools/list` equals the TOOL_EFFECTS table by name *and* by
  destructive/readOnly annotation (`tools verify`). The destructive rule is copied from
  `annotationsFromEffects`, and a unit test re-reads that function so upstream changes break us.
- "decompile works": pseudocode contains the clamp constant `0x27f`/639 that we compiled into
  `rea_probe_pan`; REA's own confidence/authority fields are reported, not trusted as proof.
- "auto picks an engine": it does not when two are usable; the e2e asserts `ambiguous` for real.
- "dependency installed": exits 0 on its version command, not "file exists".

## Local gotchas found while building it

- **Huorong (火绒) SIGKILLs freshly written Mach-O files at their original path** (exit 137, no
  system log): esbuild, turbo, TS7's native `tsc`, jscpd. A byte-identical copy at the same path runs.
  `deps unkill` replaces each with a fresh-inode copy; `deps build` does it between `npm ci` and build.
- `git grep -E` has no `\s`/`\b`: patterns silently match nothing. Use `-P`.
- `process.exit()` right after `console.log` truncates a pipe at 64/128 KB (the 134 KB catalog).
- `brew install` of several formulae in parallel can lose the openssl@3 lock and relink `python3`
  to a new minor; it also bumped `simdjson`, breaking the default `node` until `brew upgrade node`.
