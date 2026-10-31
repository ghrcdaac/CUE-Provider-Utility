# Driving `cue-upload` from the desktop app

Internal contract between the CUE desktop app (Rust core) and the bundled CLI.
Terminal users don't need any of this; the options below are hidden from `--help`
(`CUE_UPLOAD_SHOW_ALL_HELP=1 cue-upload --help` shows them).

## Upload
```
cue-upload upload --manifest <file> -c <collection> [-tp <sub_path>] \
  [-fc N -pc N] --json-events --trusted-endpoints [--allow-local] -y
```
- `--manifest`: `{"items":[{"path":"...","target_path":"optional"}]}`, max 1,000,000 items,
  exclusive with `-P`. Written by the app to its own data folder and deleted afterwards
  (avoids OS command-line limits). For a selected folder the app sets
  `target_path = "<sub_path>/<folder_name>"`; the CLI appends each file's inner relative path, so
  the destination is `<collection>/<sub_path>/<folder_name>/<inner_rel_path>` (always `/`).
- Repeated `-P` also works (one session); one `-P` behaves exactly as before.
- `--json-events`: NDJSON on stdout, see `events-v1.md`. Implies `--non-interactive`.
- Without `-y`, the CLI emits `confirm_required` after `scan_summary` and waits for `start`.
  The app normally passes `-y` (it already showed the counts on the Upload screen).
- The key is never passed in argv or env: remove `CUE_UPLOAD_API_TOKEN` from the child env.

## Retry
`cue-upload retry [--entry-id ID …] --json-events [--non-interactive]` (same events; `retry_round` per round; without `--entry-id` all entries are retried).

## stdin control channel (events mode)
One JSON object per line:
| line | effect |
| --- | --- |
| `{"cmd":"start"}` | answers `confirm_required` |
| `{"cmd":"cancel"}` | cancels the run |
| end of input | treated as cancel (keep the pipe open for the whole run) |
Unknown commands are logged as warnings and ignored. SIGINT/SIGTERM (and CTRL_BREAK on Windows)
also cancel in events mode.

On cancel the CLI stops starting new files, cancels in-flight transfers, aborts in-flight multipart
uploads, emits `file_stage CANCELLED` for unfinished files (they are **not** written to
`failed_uploads.json`), emits `batch_done` (`result_code: CANCELLED`) and exits 130.

## Exit codes (`--json-events` only; terminal mode keeps 0/1)
| code | meaning |
| --- | --- |
| 0 | all files uploaded |
| 1 | some files failed (see `failed_uploads.json` / `notice` events) |
| 2 | usage, config, or a prompt was needed in non-interactive mode |
| 3 | auth: missing key, 401 or 403 |
| 130 | cancelled |
| 70 | internal error |

## Hidden `_gui` commands (all accept `--json`)
| command | purpose / exit codes |
| --- | --- |
| `cue-upload _gui capabilities --json` | `{cli_version, protocol, features[], keyring_available, keyring_backend}` |
| `cue-upload _gui config show --json` | effective config; `api_key` shown as `"***"` |
| `cue-upload _gui config set <key> <value> [--allow-advanced]` | validated write; refuses `api_key`. Keys: `default_env`, `retry_attempts`, `auto_retry`, `file_concurrency`, `part_concurrency`, `multipart_threshold_gb`, `multipart_chunk_size_mb`, `log_level`, `user_ignored_patterns` (JSON list), `use_keyring`; with `--allow-advanced` also `environments.*`, `allowed_mime_types`, `denied_mime_types`, `failed_log_path`, `log_file_directory`. Speed presets: Gentle 2/2, Standard 4/4, Fast 8/8 (`file_concurrency`/`part_concurrency`). Invalid value → exit 2 |
| `cue-upload _gui failed list --json` | `{"entries":[…]}` from `failed_uploads.json` |
| `cue-upload _gui failed remove --entry-id ID…` | `{"removed":[…]}` |
| `cue-upload _gui targets --env E --json` | `GET /v2/me/upload-targets` with the stored key; syncs `caller.key` metadata into `credentials.json`; exit 3 = no/invalid key, 4 = `SERVER_TOO_OLD` |

Key hand-off: `cue-upload --trusted-endpoints auth set-key --env E --stdin --key-id … --name … --expires-at … --source gui`
(key on stdin only), then `auth status --env E --json` / `_gui targets --env E --json`.

## Resuming an unfinished upload (`--resume-file`, feature `resume-file`)
`upload --manifest … --resume-file <path>` appends one JSON line per finished file
(`path`, `size`, `mtime_ns`, `target`). When the same command runs again with the same
resume file (the app's "Start again" after a crash or a stopped batch), files with the same
path, size, modification time and destination are skipped and one `RESUME_SKIPPED` notice
reports how many. A line cut short by a crash is ignored (that file is uploaded again).
The app deletes the file after a finished run (exit 0 or 1) and prunes week-old ones.
Only pass it when `_gui capabilities` lists `resume-file`.
