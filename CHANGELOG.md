# Changelog

## 1.4.0 (unreleased)
**No breaking changes.** Existing commands, flags, defaults, prompts and exit codes keep working.

### Added
- OS keyring support: one API key per environment (by API hostname), shared with the CUE desktop app.
  New key lookup step: `--token` → `CUE_UPLOAD_API_TOKEN` → **OS keyring** → `.netrc` → `config.toml`.
- `cue-upload auth set-key | status | delete-key | test`.
- `configure` can store the key in the OS keyring (default when available); key input is now hidden.
- `upload -P` can be repeated to upload several paths in one session.
- `retry --entry-id ID` retries selected failed uploads.
- `--help` lists commands in a fixed order with examples.
- Redaction of API keys, bearer tokens and presigned-URL signatures in all logs.
- Desktop-app plumbing, hidden from `--help` (`CUE_UPLOAD_SHOW_ALL_HELP=1` shows it): `--json-events`,
  `--manifest`, `--non-interactive`, `--trusted-endpoints`, `--allow-local`, `_gui …` commands
  (see `docs/gui-integration.md` and `docs/events-v1.md`).
- Standalone one-dir binaries for Windows, macOS (arm64/x64) and Linux.

### Changed
- TLS verification uses the OS trust store (via `truststore`); `CUE_UPLOAD_CA_BUNDLE` can point to a PEM bundle.
- A one-time warning is shown when `config.toml` points an official environment at another host.
- When the shared `config.toml` key is used for an environment other than `default_env`, a warning is shown.

### Fixed
- Declining "Proceed with upload?" now reports "Upload cancelled by user." instead of an unexpected error.
- HTTP status codes from the backend are kept on API errors (no duplicate "unexpected error" log line).
- `__version__` now matches the package version.
