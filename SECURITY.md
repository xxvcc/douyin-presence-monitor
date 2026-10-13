# Security and privacy

This application stores activity estimates for explicitly configured accounts.
Use only with your own accounts or people who consent. It does not bypass the
platform's relationship/privacy checks, collect chats, or guarantee invisibility.
The website-internal API is not a stable public API and may stop working.

Never post Cookie/sessionid/Bot Token, backup archives, a real config, or an
unredacted journal in a public issue. Rotate exposed credentials at their source.
Credential prompts require a trusted local terminal; no secret command-line flags.

Runtime is non-root. Native systemd uses the fixed OS account/group
`douyin-monitor`. The application root (default `/opt/douyin-presence-monitor`)
contains releases, launcher, config, data, logs, backups, exports, docs and the real
unit; only OS account registration and systemd registration links live outside.
Code/current/unit are root-owned. Data/logs/backups/exports directories are 0700
service-owned. Ordinary config is root:service-group 0640 in a 0750 directory;
secret sources remain root:root 0600 and ONLY secrets use LoadCredential. The
service reads the real config after acquiring its instance lock, not a stale
LoadCredential configuration snapshot. Never make secret source files group-readable.
A systemd-provided 0440 file is accepted only in the exact CREDENTIALS_DIRECTORY
under /run/credentials, on a read-only mount with the expected file type/owner.
This exception does not apply to ordinary files or a spoofed writable directory.

The fresh offline installer refuses existing roots, symlink destinations and
unsafe path spellings. It never initializes credentials or starts the daemon;
account creation and systemd link are only on explicit real `--systemd` installs.
Use trusted administrator-controlled sources and wheels, verify release hashes;
ZIP metadata checks do not make an untrusted wheel safe to execute. Installation
parents must be trusted; do not race another privileged writer. Failed installs
leave their new root for review rather than recursively deleting files.

Docker uses private, read-only config mounts under `/app`; all app files stay in
that root and host durable folders share one instance root. The container UID
owns secrets at 0600; the setup override permits config writes only for explicit
maintenance. Docker image layers/metadata are separately Docker-managed, not in
the instance data directory. Rootfs remains read-only, capabilities dropped, no
published ports or daemon socket. Tokens are plaintext in access-controlled source
files, NOT claimed encrypted at rest. Protect the host and backups. Database-only
exports omit credentials but still contain private activity and destinations.

Transport verifies TLS, refuses automatic redirects, limits response size and
does not inherit shell proxies. Douyin is direct; Telegram proxy is explicit.
No public management port, automatic login, telemetry or central project server.

Backup restoration and notification/identity changes require a stopped service.
Cross-file/SQLite updates cannot be made magically atomic: recovery markers,
verified snapshots and fail-closed bindings protect interruptions. Never manually
change the bound account/bot to force a pending queue through.

Report security issues privately through a maintainer-approved channel. Do not
publish credentials, private activity records or sensitive exploit details in
ordinary GitHub issues. No dedicated private reporting address is listed yet.
A public report must use synthetic IDs and placeholder secrets only.
