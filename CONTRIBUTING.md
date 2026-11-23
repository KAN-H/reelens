# Contributing

## Ground rules

1. **"Measured" is not "correct".** Any new measurable field must say how it was measured
   and whether the method is calibrated; uncalibrated proxies are `DERIVED`, never
   `MEASURED`. See `python/src/reelens/status.py`.
2. **Do not fill gaps with zeros.** If something cannot be measured, it is `UNKNOWN` or
   `SKIPPED` with a reason and shows up in `unknowns[]`.
3. **The DSH plugin is a shell.** Business rules live in the Python CLI. `execute()` returns
   canonical JSON; `output.render()` is presentation only (PTC mode never sees it).
4. **Every claim in a doc needs a measurement behind it.** If you add "faster" or "more
   accurate", put the number and the command in `scripts/gates/`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e python[all] pytest
python -m pytest python/tests -q          # no network, no ffmpeg needed
node test-host.mjs && node test-client.mjs && node test-load.mjs
```

The three Node suites check the DSH contracts: route/tool names, path traversal guards,
`planId` sensitivity, the client bundle's six hard rules, and that `apply()` really
registers six tools and seven routes against a fake context.

If you change `client.js`, remember that a bundle which forgets `return module.exports`
takes down the **entire** client plugin tree while the host log stays clean —
`node test-client.mjs` catches the common cases before you open a pull request.

## Adding a measurement

1. Implement it in `python/src/reelens/`, returning `status.field(...)`.
2. Add a gate under `scripts/gates/` that measures it on real material and records what it
   can and cannot see.
3. Add the field to `REQUIRED` in `report.py` only if a report without it is useless.
4. Update `README.md` "What it measures" and, if it needs an external service, `PRIVACY.md`.

## Adding a vision provider

Providers implement one call shape (`ask(provider, image_path, prompt)`). New providers
must pass `vision-probe` (four known-answer images including a blank negative) or they are
never inserted into the chain. Do not add a provider that cannot be probed.

## Commit style

One subject line, imperative, naming the part (`shots: …`, `vision: …`, `plugin: …`). If a
change alters a measured value or a threshold, include the before/after numbers in the body.