# `cue-upload` event stream, schema v1

Used by the CUE desktop app. Terminal users never see it.

## How it works
- `cue-upload upload … --json-events` and `cue-upload retry … --json-events` (hidden flag).
- **stdout** carries only newline-delimited JSON, one event per line, flushed per line.
- Human output (Rich) is muted; the log file is still written as configured.
- Events mode never prompts on the terminal.
- Secrets never appear in events: notice texts and all log records pass through `redact.py`
  (`Bearer …`, `cue_sk_…`, `X-Amz-Signature/Credential/Security-Token`, `refresh_token`/`access_token`/`id_token` values).
- The pydantic models in `cue_provider_utility/events.py` (`EVENT_MODELS`, `validate_event`) are the
  machine-readable version of this table; tests validate every emitted line against them.

## Common fields (every event)
| field | meaning |
| --- | --- |
| `v` | schema version, always `1` |
| `ts` | ISO 8601 UTC timestamp (ms) |
| `session_id` | one UUID per CLI run (also the `session_id` of failed-log entries written by this run) |
| `type` | event type (below) |

Optional fields are omitted when they have no value.

## Event types
| type | fields |
| --- | --- |
| `hello` | `cli_version`, `protocol: 1`, `pid`, `env`, `api_host` |
| `scan_item` | `item_id`, `path`, `rel_path`, `size`, `status: include\|ignored\|rejected\|empty`, `reason_code?` |
| `scan_summary` | `files`, `bytes`, `ignored`, `rejected`, `empty` |
| `confirm_required` | `files`, `bytes` (waits for `{"cmd":"start"}` or `{"cmd":"cancel"}` on stdin) |
| `batch_start` | `files`, `bytes`, `collection`, `target_path?`, `file_concurrency`, `part_concurrency` |
| `file_stage` | `file_id`, `path`, `stage`, `remote_file_id?`, `attempt?`, `max_attempts?` |
| `file_progress` | `file_id`, `bytes_done`, `bytes_total`, `part?`, `parts?` (≤ 4/s per file, plus a final one at 100%) |
| `file_done` | `file_id`, `path`, `remote_file_id`, `sha256_b64`, `size`, `content_type`, `remote_path` (`collection/target/rel`), `duration_s` |
| `notice` | `code`, `severity`, `title`, `text`, `notify`, `retryable`, `hint?`, `file_id?`, `failed_entry_id?` |
| `retry_round` | `round`, `max_rounds`, `files` |
| `batch_done` | `succeeded`, `failed`, `cancelled`, `bytes`, `duration_s`, `result_code: BATCH_OK\|BATCH_PARTIAL\|CANCELLED` (always the last event) |

`file_id` is a local UUID per file path, stable across auto-retry rounds within one run.

### Stages
`HASHING → NEGOTIATING → UPLOADING → VERIFYING → COMPLETED`, or `FAILED`.
With auto-retry on, a failed file is `RETRY_PENDING`; each end-of-session round emits `retry_round`
and `RETRYING` per file; `FAILED` (with its `notice`) is emitted only after the last round.
`CANCELLED` is used when the user cancels (see the control channel in `gui-integration.md`).
Multipart files skip `HASHING` (the checksum runs in parallel with the transfer).

## Notices (message catalog)
All codes, titles, texts and hints live in `cue_provider_utility/messages.py` (`CATALOG`).
`notify: true` marks the few notices the GUI should surface as a toast/desktop notification
(batch finished/partly failed, cancelled, key problems, fatal errors). Per-file successes are silent.

| severity | codes |
| --- | --- |
| success | `UPLOAD_OK`, `BATCH_OK`, `RETRY_OK` |
| info | `RESUME_SKIPPED` (`count`: files finished by an earlier run, see `--resume-file`), `SCAN_DONE`, `SKIPPED_IGNORED`, `SKIPPED_EMPTY`, `CANCELLED` |
| warning | `BATCH_PARTIAL`, `KEY_EXPIRING`, `CONFIG_KEY_SHARED` |
| error | `AUTH_MISSING`, `AUTH_INVALID`, `FORBIDDEN`, `COLLECTION_NOT_FOUND`, `COLLECTION_INACTIVE`, `PROVIDER_DISABLED`, `NOT_ASSIGNED`, `SERVER_BUSY` (HTTP 5xx/429, retryable), `NETWORK`, `TIMEOUT`, `S3_PUT_FAILED`, `CHECKSUM_FAILED`, `FILE_READ`, `FILE_TYPE_DENIED`, `CONFIG`, `SERVER_TOO_OLD`, `UPLOAD_FAILED`, `INTERNAL` |

Compatibility: new event types and new optional fields may be added within v1; consumers must ignore
unknown types and fields. Removing or renaming a field requires `v: 2`.
