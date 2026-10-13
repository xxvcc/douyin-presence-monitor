# Changelog

## 0.1.0

Initial self-hosted release. Configuration and SQLite schema use version 2.
Validation scope and reproducible checks are documented in docs/validation-0.1.0.md.

- Modular timestamp-only monitor for one observer and explicit multiple targets.
- CLI-first management; no web UI or HTTP management server.
- Versioned configuration and transactional migration of supported legacy schemas.
- Durable destination-bound Telegram outbox and independent sampling/delivery.
- Dedicated app/error/audit logs with secret redaction and retention controls.
- Initialization, target management, diagnostics, export, backup and restore.
- Single installation root selected by DPM_HOME, with versioned releases, stable
  in-root launcher, config/data/logs/backups/exports, docs and an in-root unit.
- Real offline-wheel native installer with side-effect-free dry-run, nonroot
  --no-systemd mode and explicit OS account/service-link registration; refuses
  existing roots and symlink/traversal destinations, never initializes or starts.
- Static nonroot systemd account; root-only secret source files via LoadCredential,
  ordinary config read from its real 0640 source after the instance lock.
- Non-root Docker /app layout, one host instance root, private bind mounts,
  read-only rootfs, no ports and a maintenance-only writable-config override.
- Documented matched-data migration, interrupted-recovery handling and rollback;
  packaging validation does not imply production or live-notification acceptance.
- Existing timeout-debounce and unknown-state semantics retained.
- Scoped support for systemd read-only 0440 credential mounts; ordinary secret
  files remain 0600.

Do not downgrade the executable over a migrated database. Restore a matched
program/config/database backup as described in docs/operations.md.
