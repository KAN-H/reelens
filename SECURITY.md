# Security

## Threat model

This project runs local measurements and exposes them to a DSH session over localhost.
The interesting surface is therefore: the local HTTP routes, the subprocess boundary, and
whatever the configured vision providers receive.

### Local HTTP routes

- All routes are registered on the DSH web server under `/api/video-breakdown/*`. They
  are meant for the same origin only: a request carrying a foreign `Origin` is rejected
  with 403.
- Write routes (`plan`, `run`) additionally require the header
  `x-video-breakdown-action: 1`, so a cross-site form post cannot start a job.
- `job_id` is validated against `^[A-Za-z0-9._-]{1,80}$` and resolved under the data root,
  with an explicit prefix check after resolution. `..`, separators and absolute paths are
  rejected.
- The frame route serves only `*.jpg|jpeg|png` **inside** `<job>/frames`, with the same
  prefix check. It never serves arbitrary paths.
- No route reads or writes outside the data root.

### Subprocess boundary

- The Python CLI is invoked with an argument array (no shell), so filenames with spaces or
  quotes cannot inject commands.
- **Exception**: the optional vision "agent CLI" provider expands a command template and
  runs it through a shell. That template comes from *your* environment
  (`BREAKDOWN_OCP_CMD` / `BREAKDOWN_WB_CMD`) and is never derived from media content.
  Treat it like any other shell alias you define; leave it unset if you do not need it.
- The CLI runs with a timeout and returns JSON on failure; it does not print tracebacks to
  the caller.

### Credentials

Keys are read at call time from the environment (CLI) or from the DSH credentials service
(plugin) and injected into the subprocess. They are never written to the report, the job
log, or the probe cache. The probe cache stores only pass/fail and timings.

### Untrusted input

Video titles, transcripts and on-screen text are untrusted content. They are written into
the report as data. The plugin does not execute or evaluate any text produced by a model;
vision output is coerced into a fixed schema (enums and 0–1 ratios) before it is stored.

## Reporting a vulnerability

Please open a private security advisory on the repository (Security → Report a
vulnerability) rather than a public issue. Include the version, a minimal reproduction and
the affected route or command. We will acknowledge within a few days.

## Out of scope

- Content downloaded from third-party sites: availability, terms of service and
  authentication are the operator's responsibility.
- The accuracy of transcription or vision output (see the status semantics in the README).
