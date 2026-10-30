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
`cue-upload retry --json-events [--non-interactive]` (same events; `retry_round` per round).

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
