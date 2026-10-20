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

- Gateway in the web UI. A server with the gateway protocol gets a "Gateway" section on its card: a summary (client
  subnet, port, container check, healthy exits, clients with and without an exit), the exits table (slot, state, health and handshake
  age, weight, client limit, assigned clients) with add / change / remove (removal that would leave clients without an
  exit asks again, naming how many, and "Remove anyway" repeats it with `force`), the clients and their exits (move to another
  exit, pin / unpin, assign an exit to a client who has none, "Check now"), and "Russia direct" (the active list, "Update
  from RIPE", "Upload file", a candidate list waiting for confirmation, exceptions with plain explanations of the backend's
  refusals, "Apply on the gateway"). The gateway is installed and removed next to AmneziaWG (checklist step, maintenance,
  danger zone): a server with only the gateway no longer counts the protocol step as done, and the port and removal belong
  to the right protocol. Granting access offers the gateway as its own item; a gateway client's card shows its exit. New audit
  events and detail keys (a "Gateway" filter group) and task types are translated.
- Gateway: failover and manual control of clients. A scheduler rule (`gateway_watch`, every minute) reads the exit tunnels;
  an exit counts as failed after two bad checks in a row (stale handshake or missing tunnel) and its
  clients move to the healthy exits at once (weights and client limits respected); with no healthy exit nothing changes and the
  clients stay without internet (kill-switch). Unpinned clients stay where they moved; pinned ones return to their exit after it
  has been healthy for 10 minutes (at most 5 per pass). `PATCH /api/v1/servers/{id}/gateway/assignments/{credential_id}`
  moves a client to a chosen exit and/or pins it, `POST /api/v1/servers/{id}/gateway/check` checks now. New audit events
  `gateway_client_moved`, `gateway_failover`, `gateway_exit_recovered`. If every exit looks bad at once (gateway restart, total
  outage) nothing is counted or moved and one `gateway_all_exits_down` event is written. Gateways are polled in parallel with a
  20 s limit each. Not done: load rebalancing, automatic tunnel restart, conntrack cleanup.
- Gateway: Russia goes direct. Clients of a gateway reach Russian addresses from the gateway's own address; everything else
  still goes through the assigned exit. The list comes from RIPE (`delegated-ripencc-extended-latest`, https only, size
  limit), is checked strictly (record and prefix thresholds, nothing that overlaps private or reserved ranges, nothing larger than /12) and is applied
  atomically on the gateway: a spare routing table is filled with one `ip -batch` and then switched, so clients never see a
  moment without a rule, and a failed apply leaves the working table untouched. A list that differs from the active one by
  more than 20% (or that replaces a fifth of the covered addresses with foreign ones) waits for an admin (`POST /api/v1/geoip/{id}/activate`). `GET /api/v1/geoip`, `POST /geoip/update`
  (weekly on the scheduler while a gateway exists), `POST /geoip/upload` (fallback without access to RIPE), per-network
  overrides `GET/POST/DELETE /geoip/overrides` (`direct` / `tunnel`), `POST /servers/{id}/gateway/ru/apply`. Sync, health and
  reinstall restore the routes. The country in RIPE is the registration country of a block: CDN-hosted Russian services and
  DNS (which goes through the exit) are not covered.
- Gateway: client traffic now goes through the exits. Each client gets an exit when its config is issued (the least loaded
  healthy exit, by weight and client limit); the mapping lives in the DB and is applied to the gateway as a whole
  (`assign.rules`, restored after a container restart). A client without an exit has no internet (kill-switch), never a
  leak. Removing an exit moves its clients to the others first; if some would be left without an exit, the removal needs `?force=true`. Established
  connections stay on their exit when a mapping changes. Sync re-applies the mapping if the gateway differs from the DB.
  `GET /api/v1/servers/{id}/gateway/assignments`, `assigned_clients` in the exit list.
- Gateway exits: `POST/GET/PATCH/DELETE /api/v1/servers/{id}/gateway/exits` connect foreign panel servers (with the
  AmneziaWG protocol) to a gateway. For each exit the panel creates an internal client on it and brings up a `gwxN`
  tunnel on the gateway (own routing table, MTU 1280, NAT on the tunnel). Adding runs in two resumable phases, removal
  hands clients over (a stub until the next step) and cleans both ends; the weight and client limit are edited without
  a job. An exit server cannot be deleted or lose its AmneziaWG while a gateway uses it; sync restores a lost internal
  client and removes orphaned ones, and reports tunnels that are down. Which clients use which exit comes next.
- Gateway protocol (`amnezia_gateway`), first step: a separate `vpn-admin-gw` container next to the regular protocol,
  with its own port, `gw0` interface and `10.77.0.0/22` client subnet. `POST/DELETE /api/v1/servers/{id}/protocol`
  take an optional body `{"type": "amnezia_wg" | "amnezia_gateway"}` (without it nothing changes). The container runs a
  panel-generated `start.sh`: a kill-switch rule so client traffic never leaks out of the server's own address or
  reaches the host, client isolation, TCP MSS clamping. Health reports "no exit tunnels" until exits arrive in the
  next steps. Clients are issued as for the regular protocol.
- Web interface for taking over the clients of an installed AmneziaVPN app: a server card with a found app shows an
  "AmneziaVPN is already on this server" block (container, port, state, clients and how many are new) and, with the
  `servers:manage` permission, a "Take over clients" button. The dialog picks the clients (new ones are selected;
  those already in the panel or unsupported are locked with a reason), a user name and an optional email for each,
  and one end-of-access date for all, then follows the job and shows what was taken over, what was already there,
  what disappeared and what conflicted. "Find existing Amnezia" in maintenance starts a search; next to an app's v1
  container the panel's own protocol step offers a hint instead of the install button. Users without an email show
  a dash, and the email is optional when editing them. A client taken over from the app has no "Show config"
  button, with a note that the private key stays in the app and the panel cannot issue a new client for that
  access. The audit log knows the new discovery and takeover events.
- Importing clients of an installed AmneziaVPN app into the panel: `POST /api/v1/servers/{id}/import` takes the chosen
  clients from the last discovery report and gives each one a user (an existing one, or a new one whose email may be
  empty), a subscription and an access grant. The admin sets the access period; the server does not change. The
  panel holds no config for an imported client, because its private key stays in the app, so the config cannot be
  shown. Issuing new clients in the app's container is not supported yet.
- Removing a client of an AmneziaVPN app from the panel: its peer leaves the server and its name leaves the app's
  client list, applied to the running VPN without a restart. Before writing, the panel re-reads the app's config; if
  applying fails, it restores the previous config and re-aligns the interface with the file. Returning a client's peer
  to the app's container (restore) is done just as safely. Creating new clients in the app's container is not
  supported yet.
- Finding an existing AmneziaVPN app on a server (read-only): `POST /api/v1/servers/{id}/discover` starts a job,
  `GET /api/v1/servers/{id}/discovery` returns the last report with each client's status (`new`, `in_panel`,
  `revoked_in_panel`, `unsupported`). Installing a server runs the search too. The panel does not change the app
  or its clients.
- An AmneziaVPN install that the panel did not make does not, by itself, mark the server DEGRADED.
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
