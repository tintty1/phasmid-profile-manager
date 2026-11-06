# Excel import / export

Bulk-edit profiles in a spreadsheet. For moving a *single* profile between
machines, with its cookies and its machine, use the profile archive instead
([profile-settings.md](profile-settings.md#moving-a-profile)); a spreadsheet
carries settings only.

## Export

- **Web UI:** the download icon in the toolbar on the Profiles page.
- **API:** `GET /api/v1/profiles/export/excel`

Each row is a profile. The header cells carry help text as comments, and the
status, OS, WebRTC policy and proxy type columns have dropdown validation, so the
file doubles as a template.

> **The file contains proxy passwords in clear text.** It has to, for import to
> restore them. Store it as carefully as you would store the passwords. The UI
> says so before the download starts.

## Import

- **Web UI:** the upload icon in the toolbar.
- **API:** `POST /api/v1/profiles/import/excel` (multipart file upload, `.xlsx`
  or `.xls`)

Import **always creates new profiles** with fresh IDs; it never updates
existing ones. The response reports how many were created and any per-row
errors. Columns are found by their header text, so they can be reordered or
removed; only *Profile name* is required.

Imported profiles have no machine: the spreadsheet does not carry one, and
generating a hundred machines would make the import wait on the generator for each
row. Each profile gets its machine on its first launch, from the row's OS and
GPU, placed at its exit address.

## Columns

| Column | Notes |
| ------ | ----- |
| Profile ID | Written on export, ignored on import. |
| Profile name | Required. |
| Group | The group's ID, as exported. Empty for none. |
| Status | `active`, `inactive`, `blocked`, `maintenance`. Empty means `active`. |
| Operating system | `linux` or `windows`. Empty means `linux`. |
| GPU | `<vendor_id>:<device_id>`, e.g. `0x10de:0x2504`, from `GET /api/v1/fingerprints/gpus`. Empty: one is drawn from the captures. |
| Browser languages | Comma-separated, e.g. `en-US, en`. Empty: follow the proxy. |
| Timezone | IANA name, e.g. `Europe/Berlin`. Empty: follow the proxy. |
| WebRTC policy | `default`, `default_public_interface_only`, `default_public_and_private_interfaces` or `disable_non_proxied_udp`. Empty means `default`. |
| Proxy type | `http`, `https`, `socks5`. Empty for no proxy. `socks4` imports but cannot launch. |
| Proxy server | `host:port`. Empty for no proxy. |
| Proxy username / Proxy password | Optional. |
| Notes | Free text. |

What each setting does is in [profile-settings.md](profile-settings.md#settings).
