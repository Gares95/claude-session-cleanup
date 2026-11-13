# Agensi package

The [Agensi](https://www.agensi.io) skill package is a generated distribution
of this repository. The repository stays the canonical, MIT-licensed source;
the package adds no features and no code of its own.

## Why it is generated

Agensi delivers a skill's text files to the buyer's agent, which writes each
script unchanged to a temporary file, runs it, and removes it. A script
therefore cannot import other files of its package, so the normal skill
entry point (`scripts/cleanup_sessions.py`, which imports
`claude_session_cleanup/`) cannot run there.

`tools/build_agensi.py` generates one standalone, readable script from the
canonical modules instead of maintaining a second implementation. The
modules are concatenated in dependency order and are otherwise verbatim,
except that:

- package-internal imports and the per-module
  `from __future__ import annotations` are removed (one future import heads
  the script);
- references through package-module aliases (`deletion.lock`,
  `processes_module.scan`) become plain names (`lock`, `scan`);
- a few top-level names are renamed with their module's name, because more
  than one module defines them (`report.iso`, `deletion.iso`) or because a
  local name would hide them where another module refers to them
  (`processes.scan`, used in a function with a local `scan`). The script's
  header lists every rename.

The build fails unless:

- each section's syntax tree equals the canonical module's tree after
  exactly those changes;
- every rewritten name still resolves to the global it meant (checked with
  `symtable`), and no global lookup in a module, such as a builtin, would
  now find another module's name;
- the script imports only reviewed standard-library modules and contains no
  `exec`, `eval`, `compile`, `__import__`, `os.system`, process creation,
  `shell=`, permission changes, or long encoded-looking literals;
- `packaging/agensi/SKILL.md` has frontmatter with the package's name and
  the current version.

A new import in the canonical source fails the build until it is reviewed
and added to `ALLOWED_IMPORTS`.

## Building

```bash
python3 tools/build_agensi.py
```

This writes `dist/agensi/claude-session-cleanup-<version>.zip` (ignored by
Git) and prints its contents and SHA-256. Identical sources give an
identical archive: files are sorted, timestamps fixed, permissions `0644`,
line endings LF, and there are no directories, symlinks, or extra fields.

| Packaged file | Source |
|---|---|
| `SKILL.md` | `packaging/agensi/SKILL.md` |
| `scripts/cleanup_sessions.py` | generated from `claude_session_cleanup/` |
| `references/safety.md` | `docs/safety-model.md` |
| `LICENSE` | `LICENSE` |

The script is about 150 KB. Agensi suggests keeping each script's logic
under 20 KB and splitting larger logic across scripts. Splitting would not
help here: every operation needs discovery, activity detection, the policy
engine, and the report, so each script would repeat most of the code. One
script, organized by its canonical modules, is easier to review.

## Testing

```bash
python3 -m unittest discover -s tests -v   # includes tests/test_agensi_package.py
python3 tools/test_agensi_bundle.py        # the whole suite against the packaged script
```

Both build the ZIP into a temporary directory and test the script
extracted from it:

- `tests/test_agensi_package.py` checks the archive layout, determinism,
  frontmatter, and the security gates. It runs the canonical package and
  the packaged script on the same synthetic data, on Linux, macOS, and
  Windows code paths, and requires identical text and JSON output, statuses,
  reasons, capabilities, and saved plans. It reruns the deletion test
  classes through the packaged script, and runs the script as Agensi does:
  copied alone to a temporary file and run with `python3 -I`, including a
  complete save, apply, and replay on Linux.
- `tools/test_agensi_bundle.py` makes `claude_session_cleanup` and its
  modules resolve to the extracted script, then runs every test, including
  those that patch module functions.

CI runs both on Linux, macOS, and Windows with Python 3.10 to 3.13.

## Releasing to Agensi

Build from a release tag, run both test commands, review the extracted
files, and upload the ZIP. Agensi blocks re-uploading an identical ZIP, so
an update needs a new version.
