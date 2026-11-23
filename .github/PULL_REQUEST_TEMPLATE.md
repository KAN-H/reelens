## What changed

<!-- One or two lines. If it changes a measurement, say which status it now claims. -->

## Why

## Checks

- [ ] `python -m pytest python/tests -q` passes
- [ ] `node test-host.mjs && node test-client.mjs && node test-load.mjs && node test-ptc.mjs` passes
- [ ] If a measurement changed: the report labels it honestly (method + params + status)
- [ ] If docs were updated: they describe the code as it is, not as it was
