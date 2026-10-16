---
name: dashscope-transcribe
description: Transcribe local audio recordings into text with Alibaba Cloud Model Studio (DashScope) Qwen ASR. Use when the user hands over recording files (meetings, calls, voice memos, 录音转文字) and wants a plain-text transcript.
metadata:
  short-description: Transcribe recordings to text with DashScope
---

# DashScope Transcribe

Turn local audio recordings into plain-text transcripts through the Alibaba
Cloud Model Studio (DashScope) whole-file transcription API. It uses the Qwen
audio ASR model `qwen-audio-3.0-asr-flash-filetrans` with speaker diarization
enabled and works with Python 3.9+ and the standard library only.

The recording is uploaded to Alibaba Cloud for processing, and DashScope bills
the account by audio duration. Say this to the user before the first
transcription of a file.

## One-time setup

Ask the user for the directory that holds their recordings and for their
DashScope API key, then run:

```bash
python scripts/transcribe.py setup --workdir "<recordings-dir>" --api-key "<key>"
```

- Saves the configuration to `~/.dashscope-transcribe/config.json` and then
  verifies the key without creating a billed task.
- Without `--api-key` the script prompts, or reads `DASHSCOPE_API_KEY`.
- Afterwards never print, echo, paste, or copy the key again.

## Transcribe

```bash
python scripts/transcribe.py transcribe "<audio-file-or-directory>"
python scripts/transcribe.py transcribe
```

- With no path, it processes every supported audio file in the configured
  working directory (not recursive).
- Supported input: `aac, amr, avi, flac, flv, m4a, mkv, mov, mp3, mp4, mpeg,
  mpga, ogg, opus, wav, webm, wma`.
- Writes `<workdir>/transcripts/<name>.txt` (one utterance per line, prefixed
  with `speaker_N:` when the service detected speakers) and `<name>.json`
  (language, duration, and timed segments for downstream use).
- Files that already have a transcript are skipped, so rerunning the command
  is safe.

| Flag | Use it when |
|---|---|
| `--timestamps` | The user wants `[HH:MM:SS]` prefixes in the text file |
| `--out-dir <dir>` | Transcripts should not go to `<workdir>/transcripts` |
| `--timeout-minutes <n>` | The agent cannot wait this long (default 240) |
| `--force` | The user explicitly wants a file redone; this bills again |

## Regions and endpoints

The API root is configurable, so the upload and the asynchronous task can run
in the region closest to the machine holding the recordings. Set it with
`setup --api-root` or the `api_root` field in the config file; the service
decides the temporary upload target from that region.

| Region | API root | File transcription |
|---|---|---|
| China (Beijing) | `https://dashscope.aliyuncs.com/api/v1` | Yes |
| Singapore | `https://dashscope-intl.aliyuncs.com/api/v1` | Yes, same models |
| US (Virginia) | `https://dashscope-us.aliyuncs.com/api/v1` | No: non-realtime ASR there only serves `qwen3-asr-flash` (short audio), not this file-transcription model |
| Workspace-specific | `https://{WorkspaceId}.<region>.maas.aliyuncs.com/api/v1` | Follows the workspace's service deployment scope |

```bash
python scripts/transcribe.py setup --workdir "<dir>" --api-key "<region key>" --api-root "https://dashscope-intl.aliyuncs.com/api/v1"
python scripts/transcribe.py check
```

- Alibaba Cloud API keys are region-scoped: a key from the China site
  (`bailian.console.aliyun.com`) is rejected by the international endpoints.
  Create the key in the console for the region being used
  (`modelstudio.console.alibabacloud.com` for Singapore and other
  international regions).
- The model is pinned to `qwen-audio-3.0-asr-flash-filetrans`; pick a region
  that serves it. Only the API root is user-configurable, and untrusted hosts,
  plain HTTP, custom ports, credentials, or query strings are rejected before
  any request is sent.
- Switching regions never changes an existing transcript; it only affects new
  uploads and new tasks.

## Interruptions and long recordings

Whole-file transcription is asynchronous: a one-hour recording can take
several minutes or longer. Progress goes to stderr; keep the command running
while the agent session allows it.

If a run is interrupted, times out, or the session is lost, rerun the exact
same command. Recovery state under `~/.dashscope-transcribe/state/` makes the
script resume polling the existing task instead of uploading and submitting
again, so never start a fresh transcription by hand for the same file.

`--force` discards that protection. Use it only with the user's consent. When
the script reports a submission with an unknown outcome, ask the user first:
the previous attempt may already be billed.

## When something fails

| Message | Meaning | Action |
|---|---|---|
| `InvalidApiKey`, HTTP 401 | Key is wrong or expired | Rerun setup with a fresh key |
| `Arrearage` | Alibaba Cloud account issue | Tell the user; only they can fix it |
| `Throttling`, HTTP 429 | Service rate limit | Wait and rerun the same command |
| `not a supported audio format` | Extension is not in the list above | Ask the user to convert (for example to `wav` or `mp3`) |
| `larger than the DashScope temporary upload limit` | File exceeds the upload cap | Ask for a smaller or split recording |
| `ended as failed (code ...)` | The service rejected the audio | Report the code; do not retry blindly |

Wire protocol, task states, and recovery stages are documented in
[references/dashscope-file-transcription.md](references/dashscope-file-transcription.md).
Read it when diagnosing task failures or before changing the script's
protocol behavior.
