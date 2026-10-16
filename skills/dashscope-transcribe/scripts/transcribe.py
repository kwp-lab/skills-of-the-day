#!/usr/bin/env python3
"""Transcribe local audio recordings to text with Alibaba Cloud DashScope.

Standalone CLI used by the dashscope-transcribe Agent Skill. The wire protocol
mirrors the DashScope file-transcription adapter of the Nota desktop client
(src-tauri/src/asr/providers/dashscope.rs):

  1. GET  {api_root}/uploads?action=getPolicy&model=...  model-bound upload policy
  2. POST {upload_host}                                  temporary OSS object
  3. POST {api_root}/services/audio/asr/transcription    asynchronous task
  4. GET  {api_root}/tasks/{task_id}                     poll task status
  5. GET  {transcription_url}                            download and normalize

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

MODEL = "qwen-audio-3.0-asr-flash-filetrans"
DEFAULT_API_ROOT = "https://dashscope.aliyuncs.com/api/v1"
CONFIG_VERSION = 1
STATE_VERSION = 1
USER_AGENT = "dashscope-transcribe-skill/1.0"

MAX_CONTROL_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
CONTROL_TIMEOUT_SECONDS = 60
SUBMIT_TIMEOUT_SECONDS = 120
UPLOAD_TIMEOUT_SECONDS = 30 * 60
RESULT_TIMEOUT_SECONDS = 5 * 60
TEMPORARY_OBJECT_HOURS = 48
MAX_CONSECUTIVE_POLL_ERRORS = 5
DEFAULT_TIMEOUT_MINUTES = 240
DEFAULT_POLL_SCHEDULE = (5, 5, 5, 5, 5, 5, 10, 10, 10, 15, 20)
UPLOAD_CHUNK_BYTES = 256 * 1024

SUPPORTED_EXTENSIONS = {
    "aac", "amr", "avi", "flac", "flv", "m4a", "mkv", "mov", "mp3", "mp4",
    "mpeg", "mpga", "ogg", "opus", "wav", "webm", "wma",
}

MIME_TYPES = {
    "aac": "audio/aac",
    "amr": "audio/amr",
    "avi": "video/x-msvideo",
    "flac": "audio/flac",
    "flv": "video/x-flv",
    "m4a": "audio/mp4",
    "mkv": "video/x-matroska",
    "mov": "video/quicktime",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
    "mpeg": "video/mpeg",
    "mpga": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "wav": "audio/wav",
    "webm": "audio/webm",
    "wma": "audio/x-ms-wma",
}

TASK_STATES = {
    "PENDING": "PENDING",
    "WAITING": "PENDING",
    "RUNNING": "RUNNING",
    "SUCCEEDED": "SUCCEEDED",
    "FAILED": "FAILED",
    "CANCELED": "CANCELED",
    "CANCELLED": "CANCELED",
}

REQUIRED_POLICY_FIELDS = (
    "policy",
    "signature",
    "upload_dir",
    "upload_host",
    "oss_access_key_id",
)


class DashScopeError(Exception):
    """A user-facing failure that the CLI reports and turns into exit code 1."""


class TransportError(DashScopeError):
    """The request did not complete, so the remote side's state is unknown."""


class AmbiguousSubmitError(DashScopeError):
    """The submit call may have created a billed task; never retried blindly."""


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _connect(host, port, timeout):
    return http.client.HTTPSConnection(
        host, port, timeout=timeout, context=ssl.create_default_context()
    )


def _request(
    method,
    url,
    *,
    headers=None,
    body=None,
    body_chunks=None,
    content_length=None,
    timeout=CONTROL_TIMEOUT_SECONDS,
    max_bytes=MAX_CONTROL_BYTES,
):
    """Perform one HTTP request and return ``(status, body_bytes)``.

    ``body_chunks`` streams a large body without loading it into memory.
    """

    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise DashScopeError(f"invalid URL: {url}")
    try:
        port = parts.port
    except ValueError as error:
        raise DashScopeError(f"invalid port in URL: {url}") from error
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    if body is not None and content_length is None:
        content_length = len(body)
    connection = None
    try:
        connection = _connect(host, port, timeout)
        connection.putrequest(method, target)
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if content_length is not None:
            connection.putheader("Content-Length", str(content_length))
        connection.endheaders()
        if body is not None:
            connection.send(body)
        elif body_chunks is not None:
            for chunk in body_chunks:
                if chunk:
                    connection.send(chunk)
        response = connection.getresponse()
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = response.read(min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            raise DashScopeError(
                f"{method} {host} returned an oversized response; refusing to read it"
            )
        return response.status, bytes(data)
    except (OSError, http.client.HTTPException) as error:
        raise TransportError(f"{method} {host} failed: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def _parse_response(status, data, operation):
    try:
        value = json.loads(data.decode("utf-8")) if data else None
    except (UnicodeDecodeError, ValueError) as error:
        raise DashScopeError(
            f"{operation} returned an invalid response (HTTP {status})"
        ) from error
    if not 200 <= status < 300:
        code = _error_code(value)
        suffix = f", code {code}" if code else ""
        raise DashScopeError(f"{operation} failed (HTTP {status}{suffix})")
    if value is None:
        raise DashScopeError(f"{operation} returned an empty response")
    return value


def _error_code(value):
    if not isinstance(value, dict):
        return None
    for key in ("code", "error_code"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return _safe_code(candidate)
    nested = value.get("error")
    if isinstance(nested, dict):
        candidate = nested.get("code")
        if isinstance(candidate, str) and candidate:
            return _safe_code(candidate)
    return None


def _safe_code(value):
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    return "".join(character for character in value if character in allowed)[:80]


# ---------------------------------------------------------------------------
# Endpoint validation (mirrors the Nota client's trust rules)
# ---------------------------------------------------------------------------


def _is_trusted_api_host(host):
    if not host:
        return False
    if host == "dashscope.aliyuncs.com" or host.endswith(".dashscope.aliyuncs.com"):
        return True
    if host.endswith(".aliyuncs.com"):
        prefix = host[: -len(".aliyuncs.com")]
        if prefix.startswith("dashscope-") and "." not in prefix:
            return True
    return host.endswith(".maas.aliyuncs.com")


def validate_api_root(value):
    """Normalize an Alibaba Cloud DashScope API root ending in ``/api/v1``."""

    trimmed = (value or "").strip().rstrip("/")
    if not trimmed:
        raise DashScopeError("the DashScope API base URL must not be empty")
    parts = urlsplit(trimmed)
    if parts.scheme != "https":
        raise DashScopeError("the DashScope API base URL must use HTTPS")
    try:
        port = parts.port
    except ValueError as error:
        raise DashScopeError("the DashScope API base URL has an invalid port") from error
    if parts.username or parts.password or port is not None:
        raise DashScopeError(
            "the DashScope API base URL must not contain credentials or a custom port"
        )
    if parts.query or parts.fragment:
        raise DashScopeError(
            "the DashScope API base URL must not contain a query or fragment"
        )
    host = (parts.hostname or "").lower()
    if not _is_trusted_api_host(host):
        raise DashScopeError(
            "the API base URL must use an Alibaba Cloud DashScope or Model Studio "
            "workspace domain"
        )
    path = parts.path.rstrip("/")
    if path not in ("", "/api", "/api/v1"):
        raise DashScopeError("the DashScope API base URL must end with /api/v1")
    return f"https://{host}/api/v1"


def validate_aliyun_https_url(value, label):
    parts = urlsplit(value or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (
        host == "aliyuncs.com" or host.endswith(".aliyuncs.com")
    ):
        raise DashScopeError(f"the {label} is not a trusted Alibaba Cloud HTTPS URL")


# ---------------------------------------------------------------------------
# DashScope client
# ---------------------------------------------------------------------------


class DashScopeClient:
    def __init__(self, api_key, api_root, poll_schedule=None):
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise DashScopeError("the DashScope API key is missing")
        self.api_root = validate_api_root(api_root or DEFAULT_API_ROOT)
        self.poll_schedule = tuple(poll_schedule or DEFAULT_POLL_SCHEDULE)

    def _headers(self, extra=None):
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    def get_upload_policy(self):
        status, data = _request(
            "GET",
            f"{self.api_root}/uploads?action=getPolicy&model={MODEL}",
            headers=self._headers(),
        )
        value = _parse_response(status, data, "get upload policy")
        payload = value.get("data") if isinstance(value, dict) else None
        if not isinstance(payload, dict):
            raise DashScopeError("the upload policy response is missing its data object")
        missing = [
            field
            for field in REQUIRED_POLICY_FIELDS
            if not isinstance(payload.get(field), str) or not payload.get(field)
        ]
        if missing:
            raise DashScopeError(
                "the upload policy is missing required fields: " + ", ".join(missing)
            )
        if not isinstance(payload.get("x_oss_object_acl"), str):
            payload["x_oss_object_acl"] = ""
        if not isinstance(payload.get("x_oss_forbid_overwrite"), str):
            payload["x_oss_forbid_overwrite"] = ""
        return payload

    def upload(self, source, policy, progress=None):
        validate_aliyun_https_url(policy["upload_host"], "temporary upload URL")
        size = source.stat().st_size
        max_mb = _as_float(policy.get("max_file_size_mb"))
        if max_mb is not None and size > max_mb * 1024 * 1024:
            raise DashScopeError(
                f"the recording is larger than the DashScope temporary upload "
                f"limit ({_human_size(int(max_mb * 1024 * 1024))})"
            )
        file_name = source.name
        object_key = f"{policy['upload_dir'].rstrip('/')}/{file_name}"
        boundary = f"----dashscope-transcribe-{secrets.token_hex(16)}"
        fields = [
            ("OSSAccessKeyId", policy["oss_access_key_id"]),
            ("Signature", policy["signature"]),
            ("policy", policy["policy"]),
        ]
        if policy.get("x_oss_object_acl"):
            fields.append(("x-oss-object-acl", policy["x_oss_object_acl"]))
        if policy.get("x_oss_forbid_overwrite"):
            fields.append(("x-oss-forbid-overwrite", policy["x_oss_forbid_overwrite"]))
        fields.append(("key", object_key))
        fields.append(("success_action_status", "200"))
        rendered = []
        for name, value in fields:
            rendered.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f"\r\n\r\n{value}\r\n"
            )
        rendered.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{_escape_disposition(file_name)}"\r\n'
            f"Content-Type: {_mime_type(source)}\r\n\r\n"
        )
        preamble = "".join(rendered).encode("utf-8")
        epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")

        def chunks():
            # OSS validates the multipart sequence, so the audio part stays last.
            yield preamble
            sent = 0
            with open(source, "rb") as stream:
                while True:
                    block = stream.read(UPLOAD_CHUNK_BYTES)
                    if not block:
                        break
                    sent += len(block)
                    if progress is not None:
                        progress(sent, size)
                    yield block
            yield epilogue

        status, _ = _request(
            "POST",
            policy["upload_host"],
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": USER_AGENT,
            },
            body_chunks=chunks(),
            content_length=len(preamble) + size + len(epilogue),
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        if status != 200:
            raise DashScopeError(f"the audio upload failed (HTTP {status})")
        expires_at = datetime.now(timezone.utc) + timedelta(hours=TEMPORARY_OBJECT_HOURS)
        return f"oss://{object_key}", expires_at.isoformat().replace("+00:00", "Z")

    def submit(self, oss_url):
        body = json.dumps(
            {
                "model": MODEL,
                "input": {"file_urls": [oss_url]},
                "parameters": {"channel_id": [0], "diarization_enabled": True},
            }
        ).encode("utf-8")
        try:
            status, data = _request(
                "POST",
                f"{self.api_root}/services/audio/asr/transcription",
                headers=self._headers(
                    {
                        "Content-Type": "application/json",
                        "X-DashScope-Async": "enable",
                        "X-DashScope-OssResourceResolve": "enable",
                    }
                ),
                body=body,
                timeout=SUBMIT_TIMEOUT_SECONDS,
            )
        except TransportError as error:
            raise AmbiguousSubmitError(
                f"the submission did not complete ({error}); a billed task may exist, "
                "so it is not resubmitted automatically"
            ) from error
        value = _parse_response(status, data, "submit transcription task")
        output = value.get("output") if isinstance(value, dict) else None
        task_id = output.get("task_id") if isinstance(output, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise AmbiguousSubmitError(
                "DashScope accepted the request without returning a task_id; "
                "refusing to resubmit automatically"
            )
        return task_id

    def get_task(self, task_id):
        status, data = _request(
            "GET", f"{self.api_root}/tasks/{task_id}", headers=self._headers()
        )
        value = _parse_response(status, data, "query transcription task")
        output = value.get("output") if isinstance(value, dict) else None
        if not isinstance(output, dict):
            raise DashScopeError("the task response is missing its output object")
        raw_status = output.get("task_status")
        if not isinstance(raw_status, str) or not raw_status.strip():
            raise DashScopeError("the task response is missing task_status")
        state = TASK_STATES.get(raw_status.strip().upper(), "UNKNOWN")
        code = output.get("code")
        result_url = self._successful_result_url(value) if state == "SUCCEEDED" else None
        return {
            "state": state,
            "result_url": result_url,
            "code": _safe_code(code) if isinstance(code, str) else None,
        }

    @staticmethod
    def _successful_result_url(value):
        output = value.get("output") if isinstance(value, dict) else None
        results = output.get("results") if isinstance(output, dict) else None
        if not isinstance(results, list):
            raise DashScopeError(
                "the successful task response is missing its results list"
            )
        for item in results:
            if not isinstance(item, dict):
                continue
            subtask = item.get("subtask_status")
            if not isinstance(subtask, str) or subtask.upper() != "SUCCEEDED":
                continue
            url = item.get("transcription_url")
            if isinstance(url, str) and url:
                validate_aliyun_https_url(url, "transcription result URL")
                return url
        raise DashScopeError(
            "the task succeeded but no downloadable transcription result was found"
        )

    def download_result(self, url):
        validate_aliyun_https_url(url, "transcription result URL")
        status, data = _request(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=RESULT_TIMEOUT_SECONDS,
            max_bytes=MAX_RESULT_BYTES,
        )
        value = _parse_response(status, data, "download transcription result")
        return normalize_result(value)


# ---------------------------------------------------------------------------
# Result normalization
# ---------------------------------------------------------------------------


def normalize_result(value):
    transcripts = value.get("transcripts") if isinstance(value, dict) else None
    if not isinstance(transcripts, list):
        raise DashScopeError("the transcription result is missing its transcripts list")
    text_parts = []
    segments = []
    language = None
    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        raw_text = transcript.get("text", transcript.get("transcript"))
        if isinstance(raw_text, str) and raw_text.strip():
            text_parts.append(raw_text.strip())
        if language is None:
            candidate = transcript.get("language")
            if isinstance(candidate, str) and candidate.strip():
                language = candidate.strip()
        sentences = transcript.get("sentences")
        if not isinstance(sentences, list):
            continue
        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            text = sentence.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            start_ms = _as_u64(sentence.get("begin_time"))
            end_ms = _as_u64(sentence.get("end_time"))
            if start_ms is None:
                raise DashScopeError("a transcription sentence is missing begin_time")
            if end_ms is None:
                raise DashScopeError("a transcription sentence is missing end_time")
            if end_ms < start_ms:
                raise DashScopeError("a transcription sentence has an invalid time range")
            segments.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text.strip(),
                    "speaker": _speaker_label(sentence.get("speaker_id")),
                }
            )
    segments.sort(key=lambda segment: (segment["start_ms"], segment["end_ms"]))
    text = "\n".join(text_parts)
    if not text.strip():
        text = "".join(segment["text"] for segment in segments)
    if not text.strip():
        raise DashScopeError("the transcription result contains no text")
    properties = value.get("properties") if isinstance(value.get("properties"), dict) else {}
    duration_ms = _as_u64(properties.get("original_duration_in_milliseconds")) or 0
    if not segments:
        segments.append(
            {"start_ms": 0, "end_ms": duration_ms, "text": text, "speaker": None}
        )
    duration_ms = max(duration_ms, max(segment["end_ms"] for segment in segments))
    return {
        "text": text,
        "language": language,
        "segments": segments,
        "duration_ms": duration_ms,
    }


def _speaker_label(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return f"speaker_{max(int(value), 0)}"
    if isinstance(value, str):
        raw = value.strip()
        if raw.lower().startswith("speaker_"):
            raw = raw[len("speaker_"):]
        return f"speaker_{raw}" if raw else None
    return None


def _as_u64(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value.strip()), 0)
        except ValueError:
            return None
    return None


def _as_float(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def render_text(transcript, timestamps=False):
    segments = transcript["segments"]
    if not segments:
        return transcript["text"].strip() + "\n"
    lines = []
    for segment in segments:
        prefix = f"[{_format_timestamp(segment['start_ms'])}] " if timestamps else ""
        speaker = f"{segment['speaker']}: " if segment.get("speaker") else ""
        lines.append(f"{prefix}{speaker}{segment['text']}")
    return "\n".join(lines) + "\n"


def _format_timestamp(milliseconds):
    seconds = max(int(milliseconds), 0) // 1000
    return f"{seconds // 3600:02d}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"


def _format_duration(milliseconds):
    seconds = max(int(milliseconds), 0) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def default_config_path():
    override = (os.environ.get("DASHSCOPE_TRANSCRIBE_CONFIG") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".dashscope-transcribe" / "config.json"


def load_config(path):
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise DashScopeError(
            f"no configuration found at {path}; run setup first "
            "(python transcribe.py setup --workdir <dir>)"
        ) from error
    except OSError as error:
        raise DashScopeError(f"cannot read {path}: {error}") from error
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise DashScopeError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise DashScopeError(f"{path} does not contain a configuration object")
    if value.get("version") != CONFIG_VERSION:
        raise DashScopeError(f"{path} has an unsupported configuration version")
    workdir = value.get("workdir")
    if not isinstance(workdir, str) or not workdir.strip():
        raise DashScopeError(f"{path} does not contain a working directory")
    return value


def save_config(path, config):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as error:
        raise DashScopeError(f"cannot save the configuration to {path}: {error}") from error


def resolve_api_key(config):
    key = config.get("api_key")
    if not isinstance(key, str) or not key.strip():
        key = os.environ.get("DASHSCOPE_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise DashScopeError(
            "no DashScope API key is available; rerun setup or set DASHSCOPE_API_KEY"
        )
    return key.strip()


def mask_key(key):
    if len(key) <= 8:
        return "****"
    return f"****{key[-4:]}"


# ---------------------------------------------------------------------------
# Recovery state
# ---------------------------------------------------------------------------


def _state_path(state_dir, source):
    stat = source.stat()
    raw = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    return state_dir / f"{digest}.json"


def _read_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        return None
    return value


def _write_state(path, **values):
    values["version"] = STATE_VERSION
    values["updated_at"] = _now_iso()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as error:
        raise DashScopeError(f"cannot save the recovery state to {path}: {error}") from error


def _remove_state(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _uploaded_object_is_valid(state):
    url = state.get("oss_url")
    expires_at = state.get("oss_expires_at")
    if not isinstance(url, str) or not url.startswith("oss://"):
        return False
    if not isinstance(expires_at, str):
        return False
    try:
        when = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when > datetime.now(timezone.utc) + timedelta(minutes=5)


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Transcription pipeline
# ---------------------------------------------------------------------------


def _supported_audio_files(directory):
    found = []
    try:
        entries = sorted(directory.iterdir())
    except OSError as error:
        raise DashScopeError(f"cannot read directory {directory}: {error}") from error
    for entry in entries:
        if entry.is_file() and _extension(entry) in SUPPORTED_EXTENSIONS:
            found.append(entry)
    return found


def _extension(path):
    return path.suffix.lower().lstrip(".")


def _mime_type(path):
    return MIME_TYPES.get(_extension(path), "application/octet-stream")


def _escape_disposition(name):
    return name.replace("\\", "\\\\").replace('"', '\\"')


def _human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _ensure_supported_audio(source):
    if _extension(source) not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise DashScopeError(
            f"{source.name} is not a supported audio format (supported: {supported})"
        )


def _output_paths(out_dir, source):
    stem = source.stem
    json_path = out_dir / f"{stem}.json"
    if json_path.exists():
        existing = None
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8")).get("source_file")
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, str):
            try:
                same_source = Path(existing).resolve() == source.resolve()
            except OSError:
                same_source = False
            if not same_source:
                digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:8]
                stem = f"{stem}-{digest}"
    return out_dir / f"{stem}.txt", out_dir / f"{stem}.json"


def _write_output(path, content):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def transcribe_file(
    client,
    source,
    *,
    out_dir,
    state_dir,
    timestamps=False,
    force=False,
    timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
    poll_schedule=None,
    log=None,
):
    """Transcribe one file, resuming a previous task when one is recorded."""

    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    source = Path(source).resolve()
    if not source.is_file():
        raise DashScopeError(f"audio file not found: {source}")
    _ensure_supported_audio(source)
    out_dir = Path(out_dir)
    txt_path, json_path = _output_paths(out_dir, source)
    if not force and txt_path.exists():
        return {
            "status": "skipped",
            "source": str(source),
            "transcript_path": str(txt_path),
            "json_path": str(json_path),
        }
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = _state_path(Path(state_dir), source)
    state = None if force else _read_state(state_path)
    schedule = tuple(poll_schedule or client.poll_schedule) or (5,)

    oss_url = None
    task_id = None
    if state:
        stage = state.get("stage")
        if stage == "submitted" and isinstance(state.get("task_id"), str) and state["task_id"]:
            task_id = state["task_id"]
            log(f"resuming DashScope task {task_id} for {source.name}")
        elif stage == "submit_unknown":
            raise DashScopeError(
                "a previous submission for this file has an unknown outcome and may "
                "already be billed; rerun with --force only if the user accepts a "
                "possible duplicate transcription"
            )
        elif stage == "uploaded" and _uploaded_object_is_valid(state):
            oss_url = state["oss_url"]
            log(f"reusing the temporary upload for {source.name}")

    if task_id is None:
        if oss_url is None:
            log(f"requesting a temporary upload policy for {source.name}")
            policy = client.get_upload_policy()
            size = source.stat().st_size
            last_report = {"at": 0.0}

            def progress(sent, total):
                now = time.monotonic()
                if now - last_report["at"] >= 2 or sent >= total:
                    last_report["at"] = now
                    percent = sent * 100 // max(total, 1)
                    log(f"uploading {source.name}: {percent}%")

            log(f"uploading {source.name} ({_human_size(size)}) to Alibaba Cloud")
            oss_url, expires_at = client.upload(source, policy, progress=progress)
            _write_state(
                state_path,
                stage="uploaded",
                source_file=str(source),
                out_dir=str(out_dir),
                oss_url=oss_url,
                oss_expires_at=expires_at,
            )
        try:
            log("submitting the transcription task")
            task_id = client.submit(oss_url)
        except AmbiguousSubmitError:
            _write_state(
                state_path,
                stage="submit_unknown",
                source_file=str(source),
                out_dir=str(out_dir),
                oss_url=oss_url,
                submit_attempted_at=_now_iso(),
            )
            raise
        _write_state(
            state_path,
            stage="submitted",
            source_file=str(source),
            out_dir=str(out_dir),
            oss_url=oss_url,
            task_id=task_id,
        )

    try:
        wait_minutes = float(timeout_minutes)
    except (TypeError, ValueError) as error:
        raise DashScopeError("the wait timeout must be a number of minutes") from error
    if wait_minutes <= 0:
        raise DashScopeError("the wait timeout must be greater than zero")
    deadline = time.monotonic() + wait_minutes * 60
    started = time.monotonic()
    attempt = 0
    consecutive_errors = 0
    result_url = None
    last_reported_state = None
    last_reported_at = 0.0
    while result_url is None:
        try:
            status = client.get_task(task_id)
            consecutive_errors = 0
        except TransportError as error:
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                raise DashScopeError(
                    f"polling failed {consecutive_errors} times in a row ({error}); "
                    f"rerun the same command to keep waiting for task {task_id}"
                ) from error
            log(f"polling error ({consecutive_errors}): {error}")
        else:
            state_name = status["state"]
            if state_name == "SUCCEEDED":
                result_url = status["result_url"]
                break
            if state_name in ("FAILED", "CANCELED"):
                _remove_state(state_path)
                code = status.get("code") or "unknown"
                raise DashScopeError(
                    f"DashScope task {task_id} ended as {state_name.lower()} "
                    f"(code {code})"
                )
            now = time.monotonic()
            if state_name != last_reported_state or now - last_reported_at >= 30:
                last_reported_state = state_name
                last_reported_at = now
                elapsed = int(now - started)
                log(f"task {task_id}: {state_name.lower()} ({elapsed}s elapsed)")
        if time.monotonic() > deadline:
            raise DashScopeError(
                f"stopped waiting after {wait_minutes:g} minutes; task {task_id} "
                "is still running. Rerun the same command to keep waiting."
            )
        time.sleep(schedule[min(attempt, len(schedule) - 1)])
        attempt += 1

    log("downloading the transcription result")
    transcript = client.download_result(result_url)
    payload = {
        "version": 1,
        "source_file": str(source),
        "model": MODEL,
        "task_id": task_id,
        "created_at": _now_iso(),
        "duration_ms": transcript["duration_ms"],
        "language": transcript["language"],
        "text": transcript["text"],
        "segments": transcript["segments"],
    }
    _write_output(txt_path, render_text(transcript, timestamps=timestamps))
    _write_output(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _remove_state(state_path)
    speakers = sorted(
        {segment["speaker"] for segment in transcript["segments"] if segment.get("speaker")}
    )
    return {
        "status": "transcribed",
        "source": str(source),
        "transcript_path": str(txt_path),
        "json_path": str(json_path),
        "duration_ms": transcript["duration_ms"],
        "segment_count": len(transcript["segments"]),
        "speakers": speakers,
        "language": transcript["language"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def build_parser():
    parser = argparse.ArgumentParser(
        prog="transcribe.py",
        description="Transcribe audio recordings to text with Alibaba Cloud DashScope.",
    )
    subparsers = parser.add_subparsers(dest="command")

    setup = subparsers.add_parser(
        "setup", help="save the working directory and the DashScope API key"
    )
    setup.add_argument(
        "--workdir",
        required=True,
        help="directory that holds recordings and receives transcripts",
    )
    setup.add_argument(
        "--api-key",
        help="DashScope API key (otherwise prompted or read from DASHSCOPE_API_KEY)",
    )
    setup.add_argument(
        "--api-root",
        help=f"Alibaba Cloud API root (default: {DEFAULT_API_ROOT})",
    )
    setup.add_argument(
        "--skip-verify", action="store_true", help="save without testing the key"
    )
    setup.add_argument("--config", help="config file path")

    check = subparsers.add_parser(
        "check", help="show the saved configuration and test connectivity (no billing)"
    )
    check.add_argument("--config", help="config file path")

    run = subparsers.add_parser(
        "transcribe",
        help="transcribe audio files, a directory, or the configured working directory",
    )
    run.add_argument(
        "paths",
        nargs="*",
        help="audio files or directories (default: the configured working directory)",
    )
    run.add_argument(
        "--out-dir", help="output directory (default: <workdir>/transcripts)"
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="ignore existing transcripts and recovery state; this bills again",
    )
    run.add_argument(
        "--timestamps",
        action="store_true",
        help="prefix every transcript line with [HH:MM:SS]",
    )
    run.add_argument(
        "--timeout-minutes",
        type=float,
        default=DEFAULT_TIMEOUT_MINUTES,
        help="stop waiting after this many minutes; rerun to resume (default: 240)",
    )
    run.add_argument("--config", help="config file path")
    return parser


def _command_setup(args, config_path):
    workdir = Path(args.workdir).expanduser()
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DashScopeError(
            f"cannot create the working directory {workdir}: {error}"
        ) from error
    api_key = (args.api_key or os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if not api_key and sys.stdin is not None and sys.stdin.isatty():
        import getpass

        api_key = (getpass.getpass("DashScope API key: ") or "").strip()
    if not api_key:
        raise DashScopeError(
            "provide the API key with --api-key or the DASHSCOPE_API_KEY environment "
            "variable"
        )
    api_root = validate_api_root(args.api_root or DEFAULT_API_ROOT)
    save_config(
        config_path,
        {
            "version": CONFIG_VERSION,
            "workdir": str(workdir.resolve()),
            "api_key": api_key,
            "api_root": api_root,
        },
    )
    print(f"configuration saved to {config_path}")
    print(f"working directory: {workdir.resolve()}")
    print(f"api root: {api_root}")
    if args.skip_verify:
        return 0
    print("verifying the API key (no billing)...")
    try:
        DashScopeClient(api_key, api_root).get_upload_policy()
    except DashScopeError as error:
        print(
            f"error: the configuration was saved, but verification failed: {error}",
            file=sys.stderr,
        )
        return 1
    print("verification succeeded: the API key is accepted")
    return 0


def _command_check(args, config_path):
    config = load_config(config_path)
    api_root = validate_api_root(config.get("api_root") or DEFAULT_API_ROOT)
    api_key = resolve_api_key(config)
    print(f"config: {config_path}")
    print(f"working directory: {config['workdir']}")
    print(f"api root: {api_root}")
    print(f"api key: {mask_key(api_key)}")
    print("testing the upload policy endpoint (no billing)...")
    DashScopeClient(api_key, api_root).get_upload_policy()
    print("ok: the service is reachable and the API key is accepted")
    return 0


def _command_transcribe(args, config_path):
    config = load_config(config_path)
    api_key = resolve_api_key(config)
    api_root = validate_api_root(config.get("api_root") or DEFAULT_API_ROOT)
    workdir = Path(config["workdir"]).expanduser()
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DashScopeError(
            f"cannot open the working directory {workdir}: {error}"
        ) from error
    targets = [Path(item).expanduser() for item in args.paths] or [workdir]
    files = {}
    for target in targets:
        if target.is_dir():
            found = _supported_audio_files(target)
            if not found:
                print(
                    f"warning: no supported audio files found in {target}",
                    file=sys.stderr,
                )
            for item in found:
                files[str(item.resolve())] = item
        elif target.is_file():
            files[str(target.resolve())] = target
        else:
            raise DashScopeError(f"path not found: {target}")
    if not files:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise DashScopeError(f"no supported audio files to transcribe ({supported})")
    out_dir = (
        Path(args.out_dir).expanduser() if args.out_dir else workdir / "transcripts"
    )
    state_dir = config_path.parent / "state"
    client = DashScopeClient(api_key, api_root)
    failures = 0
    for source in files.values():
        print(f"== {source}")
        try:
            result = transcribe_file(
                client,
                source,
                out_dir=out_dir,
                state_dir=state_dir,
                timestamps=args.timestamps,
                force=args.force,
                timeout_minutes=args.timeout_minutes,
            )
        except DashScopeError as error:
            failures += 1
            print(f"error: {source.name}: {error}", file=sys.stderr)
            continue
        if result["status"] == "skipped":
            print(f"already transcribed: {result['transcript_path']}")
            continue
        print(f"transcript: {result['transcript_path']}")
        print(f"json: {result['json_path']}")
        summary = (
            f"duration: {_format_duration(result['duration_ms'])}, "
            f"segments: {result['segment_count']}"
        )
        if result["speakers"]:
            summary += f", speakers: {len(result['speakers'])}"
        if result["language"]:
            summary += f", language: {result['language']}"
        print(summary)
    return 1 if failures else 0


def main(argv=None):
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    config_path = (
        Path(args.config).expanduser() if args.config else default_config_path()
    )
    try:
        if args.command == "setup":
            return _command_setup(args, config_path)
        if args.command == "check":
            return _command_check(args, config_path)
        return _command_transcribe(args, config_path)
    except DashScopeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "interrupted; no recovery state was discarded, so rerunning the same "
            "command resumes the task",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
