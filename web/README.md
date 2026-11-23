# Tester Web

Tester Web is an independently buildable Vite multi-page frontend for the Tester control service.

## Development

```bash
npm ci
npm run dev
```

The development server listens on `http://127.0.0.1:5173`. It proxies `/api` and `/web3-bs.js` to the local Tester control service at `127.0.0.1:18082`; start that service separately when exercising login or authenticated views.

## Build

```bash
npm run build
npm run preview
```

The production build is written to `dist/` and uses `/report/control/` as its base path. The Tester service serves those built files and handles the same-origin API routes.

## Pages

- `index.html`: wallet login and test controls.
- `cases.html`: test-case mind map.
- `architecture.html`: project and dependency map.
- `assets/`: page styles and browser modules.
