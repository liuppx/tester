# Tester Web

Tester Web is an independently buildable Vite multi-page frontend for the Tester control service.

## Development

```bash
npm ci
npm run dev
```

The development server listens on `http://127.0.0.1:5173`. It serves the installed wallet SDK locally and proxies `/api` to the local Tester control service at `127.0.0.1:18082`; start that service separately when exercising login or authenticated views.

## Build

```bash
npm run build
npm run preview
```

The production build is written to `dist/` and uses `/report/control/` as its base path. The `tester-web` static artifact contains those built files and the wallet SDK. Nginx serves them from `/opt/deploy/static/tester-web/current`; only same-origin API routes go to the Tester service.

## Pages

- `index.html`: wallet login and test controls.
- `cases.html`: test-case mind map.
- `architecture.html`: project and dependency map.
- `assets/`: page styles and browser modules.
