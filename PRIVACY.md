# Privacy

## What stays on your machine

- The media file, extracted frames, audio, transcripts and the report.
- The probe cache (`vision-probe.json`) and any temporary files under `BREAKDOWN_HOME`.
- All measurements: shot detection, loudness, silence, beat analysis run locally.

By default the output directory is `<repo>/outputs` for the DSH plugin and whatever you
pass to `--out` for the CLI.

## What can leave the machine

Only **frames**, only when the visual section is enabled, and only to the provider you
configured:

| Provider | Leaves the machine | When |
|---|---|---|
| `AMDGPU_API_KEY` (free vision gateway at `developer.amd.com.cn`) | the frame images | if this key is set and the provider passes the probe |
| cloud vision | the frame images | only with `--allow-cloud` / panel checkbox |
| local VLM (`BREAKDOWN_LOCAL_VLM_BASE`) | no | stays on localhost |
| agent CLI (`BREAKDOWN_OCP_CMD` / `WB_CMD`) | depends on the CLI you configured | only if you set it |

No media file, audio, transcript, or report content is ever uploaded — only individual
JPEG frames, one request per frame.

`breakdown_plan` discloses the chain it will use ("N frames will be sent to X") *before*
anything runs. `--no-vision` disables the section entirely; it becomes `SKIPPED` and the
rest of the report is unaffected.

## Credentials

The DSH plugin never stores keys. It resolves `AMDGPU_API_KEY` / `DEEPSEEK_API_KEY` from
the DSH credentials service at call time and injects them into the Python subprocess
environment; nothing is written to disk or logged. The standalone CLI reads the same
variables from your environment.

## Telemetry

None. The package contains no analytics, no update check, and no outbound call other than
the vision providers you configure.

## Data retention

Reports and frames stay until you delete them (`reelens clean <dir>`). The probe cache
expires after 24 hours and contains only pass/fail results.