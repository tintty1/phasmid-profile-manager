# Accessibility roadmap: use without a developer toolchain

The goal is that someone who is not a developer can install the manager, install
the browser from inside it, and create a profile. Part of that is in place; the
installer is not. The [getting-started guide](getting-started.md) describes the
current path.

## In place

- A dedicated desktop entry point (`python -m phasmid_pm.desktop`) opens a
  native window. The `phasmid-pm` CLI remains for source and server
  deployments.
- A writable data folder (`$XDG_DATA_HOME/phasmid-profile-manager/`) keeps the
  database, browser profiles, the installed browser and the encryption key
  outside the source tree. A fresh desktop data folder gets a generated key; an
  existing database with a missing key is never silently given a new one.
- A private loopback server on a free port. The launcher owns its listening
  socket, refuses a second instance on the same data folder, and shuts down the
  server and the browsers when its window closes.
- A dismissible setup banner installs Phasmid in the background with visible
  status, progress, errors and retry; Settings offers **Retry download** too. A
  launch before setup says what to do instead of failing obscurely.
- Empty states lead to the first profile. Regenerating a machine and permanent
  deletion explain their cost and ask for confirmation.
- Profiles go to Trash before permanent deletion. Backups and restore are in
  Settings; a restore creates a separate profile.
- The update check backs up every profile before linking to a release.

## Still needed

1. **A packaged desktop app for Linux**, so neither Python, Node.js nor a
   terminal is needed, with the pywebview backend bundled. Then verify fresh
   install, update, reinstall, backup restore and browser launch on real
   distributions.
2. **Help with a Windows font store** for Windows profiles, which today has to be
   supplied and placed by hand (see
   [Windows profiles](profile-settings.md#windows-profiles)).
3. **A verified update download** and app replacement, once releases and their
   verification exist.
4. **Translation** of the interface, and keyboard and screen-reader audits of
   dialogs and progress states.

Docker and the CLI remain the paths for servers and advanced deployments.
