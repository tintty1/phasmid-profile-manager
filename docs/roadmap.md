# Roadmap

A short, non-binding list of what is known to be missing or wrong. The history
of the upstream project, camoufox-profile-manager, is in its own repository.

## Known gaps

- **Linux x64 only.** Phasmid ships Linux x64 builds only, so the manager runs
  only on Linux x64 hosts. Profiles may claim Linux or Windows; Phasmid does not
  support macOS profiles.
- **No packaged app.** The desktop window runs from source
  (`python -m phasmid_profile_manager.desktop`) and needs a pywebview backend installed by
  hand. There are no published wheels, PyPI package or Docker image yet.
- **Windows profiles need a Windows font store** on the host, which Phasmid does
  not ship and the user has to supply. The manager neither helps set one up nor
  passes `--phasmid-windows-fonts`; it relies on Phasmid's default path or on a
  launch's `args`. In Docker that default path is outside the `/data` volume. The
  same holds for a Widevine CDM. See
  [Windows profiles](profile-settings.md#windows-profiles).
- **No GPU in the default Docker setup.** `docker-compose.yml` passes no
  `/dev/dri`, so browsers render on SwiftShader and a hardware GPU claim is not
  backed.
- **SOCKS4 proxies are refused at launch**, because the exit lookup (httpx)
  cannot go through them. A lookup of its own would make them usable; refusing
  them when the profile is saved would at least fail earlier.
- **The update check points at a repository with no releases yet**
  (`tintty1/phasmid-profile-manager`), so it reports an error until one exists.
- **Leftovers from the upstream project**: the session cookie is still called
  `cpm_session`, and `web/package.json` still carries the upstream version
  (`0.6.0-rc.1`) while the Python package is `0.1.0`.
- **Deprecations to settle before 1.0**: the top-level `detail` in errors, the
  ignored `window_size` launch field, and the unversioned `/api` aliases. See
  [api.md](api.md#deprecated).

## Ideas

Carried over from the upstream project and still applicable:

- A proxy inventory shared between profiles.
- Cookie import from a standard format.
- Reusable profile templates, and extensions per profile.
- Tags and saved filters in the profiles list.
- Encrypted profile archives, so an export is not as sensitive as the account.
- A translated web UI.
- PostgreSQL storage for larger deployments.

The web UI stays on TypeScript 6.0 until `typescript-eslint` and Next's type
check support TypeScript 7, whose compiler has no JavaScript API.

## Not planned

- **macOS or mobile profiles.** Phasmid does not support them.
- **Shipping the browser in this project's releases.** The app installs it from
  Phasmid's releases, so the browser and its generator always match.
- **Scheduled hardware rotation.** See [scheduling.md](scheduling.md).
- Anything that facilitates violating a site's terms of service.
