# Changelog

English · [Русский](CHANGELOG.ru.md)

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Fixed

- The panel no longer touches a foreign `amnezia-awg` container. The AmneziaVPN app names its container the same
  way, so "Remove protocol" on such a server would have deleted a working VPN. A container now counts as the
  panel's only if it mounts `/opt/vpn-admin/awg`; installing or managing clients against a foreign one fails with a
  clear error, removal changes nothing, and status and health report "not installed".

### Added

- Changing your own password: `POST /api/v1/auth/change-password`, and a "Change password" button in the sidebar.
  It ends the other sessions and revokes access tokens issued before the change; wrong current passwords are
  rate-limited like sign-ins.
- Temporary passwords: an admin created with a password someone else knows (`POST /api/v1/admins`, or the console
  script with a generated password) must set their own at the first sign-in — until then the panel answers 403
  everywhere except `/auth/me`, the password change, refresh and sign-out, and the interface shows a
  "Set your own password" screen. `must_change_password: false` opts a service account out.
- `scripts.create_admin` is interactive: it asks for the email and offers a random password (shown once, only in a
  terminal, never in a file or a pipe) or your own. `--random-password`, `--only-if-empty`. The creation is
  written to the audit log (`source: cli`).
- `install.sh` — a one-command installer: checks Docker, creates `.env.prod` with random secrets, issues a TLS
  certificate (self-signed, or your own with `--cert`/`--key`), starts the panel, waits until it answers over HTTPS
  and offers to create the first administrator. Safe to re-run; an existing `.env.prod` or certificate is never
  replaced. Messages in English or Russian (`PANEL_LANG`, or the system locale).
- `./panel` — maintenance in one command: `backup` (a checked, private database dump in `backups/`, the latest 14
  kept), `restore` (replaces the data after a confirmation and a safety backup, in one transaction), `update`
  (backup, `git pull --ff-only`, rebuild and start, wait until ready) and `logs`. The encryption key is deliberately
  not part of a backup. `install.sh --no-admin` skips the administrator step (`update` uses it).
- Let's Encrypt from the installer: a certificate menu in a terminal (self-signed / Let's Encrypt / your own files), `--letsencrypt` with `--accept-le-terms` and `--le-email` otherwise. certbot is installed if
  missing, nginx is stopped for seconds during verification, renewal is automatic (hooks keep the panel's `certs/` in
  sync). If issuing fails the panel starts with a self-signed certificate. `./panel cert [status|letsencrypt|renew]`
  does the same for an installed panel. The terms are accepted by the person, never silently.
- Servers that cannot reach Docker's or npm's CDNs: `./deploy/ship-images.sh SERVER` builds the panel images on
  another computer (from the committed state) and loads them over SSH; `install.sh --no-build` and
  `./panel update --no-build` start from images already on the machine; `POSTGRES_IMAGE` / `REDIS_IMAGE` in
  `.env.prod` replace the pinned database images.
- The VPN container is created with `--restart unless-stopped`, so the VPN comes back after a server reboot.
  Installing the protocol again sets the policy on servers installed earlier (without restarting the container),
  and the health check shows `restart_policy`.

### Changed

- Password-change attempts and events (`password_changed`, `password_change_failed`) appear in the audit log.

## [0.2.0] - 2026-10-10

### Added

- English interface. A RU / EN switch in the sidebar and on the sign-in page; the language is remembered in the
  browser and, if not chosen, follows the browser's language (Russian → Russian, anything else → English).
  Dates, numbers, units and country names follow the language too.

### Changed

- The audit page address uses stable group ids (`/audit?group=servers`) instead of the Russian group names.
- Every section is loaded as its own chunk, so the main bundle stays small.

## [0.1.0] - 2026-10-10

### Added

- Country flag in a server's name: pick a country when adding or editing a server (🇩🇪 `frankfurt-1`). A bundled
  flag font makes flags render on Windows too.
- Server monitoring: CPU, memory, disk, network and clients online, collected over SSH; charts on the server
  page and the latest values in the servers table.
- Editing a VPN user's name, email and note.
- Renaming a server and editing its note.
- Panel key instead of a password: when SSH access is set with a password, the panel installs its own key on the
  server (the "Replace the password with the panel key" checkbox, on by default), verifies the login with it and
  then stores no password.
- "Renew key" and "Switch to the panel key" in server maintenance; the SSH access card shows the key fingerprint.
- Audit log: panel key installation (started / finished / failed); on deleting a server or credentials and on an
  address change — the fingerprint of the panel key left in the server's authorized_keys.
- The "Audit" page (`/audit`, for roles with the audit read permission): filters by admin, event group, object and
  dates, records loaded 100 at a time. A menu item and an "Events of this server" link on the server page.
- The admin's email instead of an id in the audit log.

### Changed

- Monitoring endpoints are now `/api/v1/monitoring/latest` and `/api/v1/servers/{id}/monitoring` (they were
  `…/metrics`): ad blockers cut off any URL containing "metrics".
- The product is named Tollwire.
- Access and config records now name the user and the server; records from before the update lack them, so the
  object is not named in the log.

### Security

- 422 responses no longer echo the submitted values: a password or key from an invalid request does not end up
  in the response.
- The key is installed only on a server with a pinned host key; if the server does not accept the key it is
  removed and the previous access stays.

[Unreleased]: https://github.com/KirillDomitin/Tollwire/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/KirillDomitin/Tollwire/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/KirillDomitin/Tollwire/releases/tag/v0.1.0
