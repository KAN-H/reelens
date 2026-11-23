# Security review — public export of `reelens`

This file records the desensitisation review performed before publishing. It is part of the
export, not of the runtime package.

Date: 2026-11-22
Source: a private development checkout (not published)
Method: allow-list copy of source files only, then a scan of the copy. No directory mirror,
no junction or symlink, fresh Git history.

## Checks performed on the exported tree

| Check | Command | Result |
|---|---|---|
| No credentials or credential-shaped strings | scan for API-key prefixes, session-cookie assignments and inline secret literals | **none found**; environment variable **names** only (`REELENS_*`, `AMDGPU_API_KEY`, `DEEPSEEK_API_KEY`) |
| No local absolute paths | scan for drive letters, `/home/<user>`, known internal roots | **none in code**; the only remaining occurrences are illustrative placeholders in the panel's input hint |
| No development records or artifacts | look for `.dev-notes/`, `.tmp/`, `.models/`, `outputs/`, venvs | **not present** (all four are also in `.gitignore`) |
| No private project identifiers | scan for the private project's name, its environment-variable prefix and internal doc paths | **removed** (reworded to a neutral description; the legacy env prefix was replaced by `REELENS_*`) |
| No vendored binaries | list of tracked files | only text sources, SVG icon, JSON locales |
| Independent history | `git log --oneline` | **1 commit**, no remote configured, so no earlier path or naming leaks remain in history |
| Tests pass in the exported tree | `node test-host.mjs`, `node test-client.mjs`, `node test-load.mjs`, `node test-ptc.mjs`, `pytest` | **14 / 9 / 9 / 10 checks + 26 tests passed** |

## Deliberate exclusions

| Excluded | Why |
|---|---|
| `scripts/gates/g3_golden.py` | compares against the private toolkit; it cannot run without it |
| `.dev-notes/` | internal gate logs, evidence media, and notes |
| `outputs/`, `.venv-breakdown/`, `.tmp/`, `.models/` | runtime state, local environments, downloaded models |

## Environment variables and credentials

The public package reads only documented `BREAKDOWN_*` variables (plus the optional
`AMDGPU_API_KEY` / `DEEPSEEK_API_KEY` for the vision chain). Names only — no values are stored or logged.

## Residual risks accepted

1. The optional "agent CLI" vision provider runs a user-supplied command template through a
   shell. It is disabled unless the user sets `BREAKDOWN_OCP_CMD` / `BREAKDOWN_WB_CMD`, and
   the template never derives from media content. Documented in `SECURITY.md`.
2. Reports contain transcripts and on-screen text from third-party media. They stay in the
   user's output directory; the package uploads nothing except frames to a vision provider
   the user configured (see `PRIVACY.md`).
