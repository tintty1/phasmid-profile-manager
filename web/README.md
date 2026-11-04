# Web UI

The Phasmid Profile Manager front end: a Next.js 16 app that FastAPI serves as a
static export, so the whole product runs as one process on one port.

## Running it

Most of the time you do not need this directory at all — `phasmid-pm` serves a
prebuilt UI. Work here only when changing the interface.

```bash
npm install
npm run dev        # http://localhost:3000, proxies /api to http://localhost:8000
```

Point the dev proxy somewhere else with `API_PROXY_TARGET`:

```bash
API_PROXY_TARGET=http://localhost:8099 npm run dev
```

To produce the bundle the Python package ships:

```bash
python ../scripts/build_webui.py
```

That runs the static export and copies `out/` into `src/phasmid_pm/webui/`.

## Structure

```
src/
  app/
    page.tsx             Profiles: the main registry, launch menu, row actions
    groups/page.tsx      Groups
    schedules/page.tsx   Scheduled launches
    trash/page.tsx       Trash: restore or delete permanently
    settings/page.tsx    Effective configuration, browser install, backups, updates
    layout.tsx           Fonts, shell and toasts
    globals.css          Design tokens and component classes
    fonts.css            IBM Plex, self-hosted through @fontsource
  components/
    app-shell.tsx        Fixed rail + work area
    profile-form.tsx     Create and edit a profile; the Machine panel and its JSON view
    browser-setup.tsx    The Install browser banner
    browser-viewer.tsx   noVNC viewer for a profile's virtual display
    recovery-settings.tsx  Backups and the update check
    login-gate.tsx       Login screen when accounts exist
    modal.tsx            Dialog shell and confirm dialog (dialogs stack)
    toast.tsx            Non-blocking notifications
    empty-state.tsx      First-run and no-results states
  lib/api.ts             Typed API client
tests/stage_one_ui.py    UI smoke tests, run by CI with Playwright's Chromium
```

## Design

The UI is an instrument panel: a dense control surface where colour carries
meaning instead of decoration.

- **One signal colour.** Orange marks the primary action and a running browser —
  nothing else. A running profile is the only thing on screen that draws the eye,
  so the table reads as a status board.
- **Hairlines, not boxes.** Rows and sections are separated by 1px rules rather
  than nested cards.
- **Monospace for machine values.** IDs, proxies and addresses are mono so they
  align and stay scannable; prose is IBM Plex Sans.
- **Restraint in motion.** Rows stagger in once on load and the running dot
  pulses. Everything respects `prefers-reduced-motion`.

Tokens live in `@theme` in `globals.css`; there is no Tailwind config file
(Tailwind v4 reads the CSS). Fonts come from the `@fontsource` packages and are
bundled with the build, so the UI works offline.
