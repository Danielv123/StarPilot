# Comma Companion web

Private React interface for the Comma Companion archive. The production build
uses same-origin `/api/v1` requests and authenticated Range URLs from the media
manifest. It never connects to the comma directly.

## Commands

```powershell
npm install
npm run dev
npm test
npm run build
```

The build output is `dist/` and is intended to be copied into the backend
container's static directory. Browser routes require an `index.html` fallback.

## Local modes

`npm run dev` proxies `/api` to `http://127.0.0.1:8080` by default. Set
`COMMA_COMPANION_API` for a different development API target.

Sample data is opt-in only:

```powershell
$env:VITE_DEMO_MODE='true'
npm run dev
```

Production builds always compile demo mode out, even if `VITE_DEMO_MODE` is set
in the environment. The build also fails if known fixture identifiers enter an
emitted JavaScript file. Failed API requests therefore remain visible as errors
and never fall back to fabricated data.

## Data contracts

- API timestamps are UTC strings. They render in browser-local
  `YYYY-MM-DD HH:mm` form.
- Drive coordinates are route-relative integer microseconds (`t_us`).
- Telemetry uses canonical dotted signal IDs.
- Media playback reads a per-camera manifest of AV1 WebM segment URLs. The
  player maps global `t_us` to segment-local media time and refuses to invent
  alignment when `start_t_us` is absent.
- Tune mode reads its controls from the model parameter schema. It submits an
  offline simulation and shows recorded, baseline, and candidate traces with
  validity warnings and provenance.
