# DashScope File Transcription Contract

Status: mirrors the verified client implementation
(`nota/src-tauri/src/asr/providers/dashscope.rs`). Update both together when
the provider contract changes.

## Endpoints

| Step | Request |
|---|---|
| Upload policy | `GET {api_root}/uploads?action=getPolicy&model=qwen-audio-3.0-asr-flash-filetrans` |
| Temporary object | `POST {data.upload_host}` with the signed OSS multipart form |
| Submit task | `POST {api_root}/services/audio/asr/transcription` |
| Poll task | `GET {api_root}/tasks/{task_id}` |
| Download result | `GET {transcription_url}` |

`api_root` is always `https://<host>/api/v1`. Only user-configured roots on
`dashscope.aliyuncs.com`, `*.maas.aliyuncs.com`, or a single-label
`dashscope-*.aliyuncs.com` prefix are accepted; custom ports, credentials,
query strings, and other paths are rejected. Upload hosts, result URLs, and
the result download must be HTTPS on `aliyuncs.com`.

## Upload policy

The policy response uses the `data` object:

`policy`, `signature`, `upload_dir`, `upload_host`, `oss_access_key_id`,
optional `x_oss_object_acl`, optional `x_oss_forbid_overwrite`, optional
`max_file_size_mb`.

The multipart form sends the text fields in this order and keeps the audio
part last, because OSS validates the sequence:

1. `OSSAccessKeyId`, `Signature`, `policy`
2. `x-oss-object-acl`, `x-oss-forbid-overwrite` (when returned)
3. `key` = `<upload_dir>/<file-name>`, `success_action_status` = `200`
4. `file` = the audio bytes with the matching content type

Success is HTTP 200; the temporary object reference is
`oss://<upload_dir>/<file-name>` and stays valid for about 48 hours.

## Task submission

Headers: `Authorization: Bearer <key>`, `X-DashScope-Async: enable`,
`X-DashScope-OssResourceResolve: enable`, `Content-Type: application/json`.

Body:

```json
{
  "model": "qwen-audio-3.0-asr-flash-filetrans",
  "input": { "file_urls": ["oss://..."] },
  "parameters": { "channel_id": [0], "diarization_enabled": true }
}
```

The response must contain `output.task_id`. A transport failure or a success
response without a `task_id` leaves the outcome unknown: the task may exist
and may already be billed, so the script records `submit_unknown` and refuses
to resubmit until `--force`.

## Task states

`output.task_status`: `PENDING`/`WAITING` (queued), `RUNNING`, `SUCCEEDED`,
`FAILED`, `CANCELED`/`CANCELLED`. Unknown values are treated as still running
until the wait timeout. On success the first result with
`subtask_status: SUCCEEDED` provides `transcription_url`; a success without a
downloadable result is an error.

## Result normalization

`transcripts[]` may carry `text` or `transcript` plus `language` and
`sentences[]` with `begin_time`, `end_time`, `text`, and optional
`speaker_id` (number or `speaker_N` string). Sentences are sorted by time and
labeled `speaker_N`. When no sentences exist, the whole text becomes one
segment spanning `properties.original_duration_in_milliseconds`. A result
without any text is an error.

## Recovery stages

State files live in `~/.dashscope-transcribe/state/<hash>.json`, keyed by the
absolute source path, size, and mtime:

```text
uploaded        oss_url + oss_expires_at -> reuse the temporary object
submitted       task_id                  -> resume polling only
submit_unknown  submit_attempted_at      -> require --force to try again
```

Successful runs delete their state file. Failed or canceled tasks also delete
it; timed-out and interrupted runs keep it so the next identical command
resumes. Reusing an upload requires more than five minutes of remaining
lifetime.
