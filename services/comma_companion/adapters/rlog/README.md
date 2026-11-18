# Comma Companion rlog adapter

This adapter turns one route's rlogs into the versioned JSON/NDJSON telemetry
contract documented in [CONTRACT.md](CONTRACT.md).

It deliberately lives outside the server backend. Its only integration surface
is the CLI contract, keeping StarPilot's generated schemas and Python imports
out of the long-lived archive service.

The stream includes visualization series, encoder-frame mappings, markers,
sentinel-verified completeness, and timestamp-causal 100 Hz dynamics rows.
Only full rlogs emit dynamics rows; qlogs remain visualization-only.

Run focused tests:

```powershell
$env:PYTHONPATH = "."
uv run --no-project --with pytest --with pycapnp==2.1.0 --with zstandard `
  pytest -q -o "addopts=" `
  --confcutdir=services/comma_companion/adapters/rlog `
  services/comma_companion/adapters/rlog/tests
```
