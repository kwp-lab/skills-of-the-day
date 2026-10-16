#!/usr/bin/env python3
"""Tests for transcribe.py. Standard library only; run with:

    python test_transcribe.py -v

The end-to-end tests run the real HTTP pipeline against a local mock of the
DashScope endpoints; only the Alibaba Cloud host allowlist is relaxed.
"""

import contextlib
import email.parser
import http.client
import http.server
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transcribe as t  # noqa: E402

RESULT_PAYLOAD = {
    "properties": {"original_duration_in_milliseconds": 8000},
    "transcripts": [
        {
            "language": "zh",
            "text": "大家好，会议开始。好的。",
            "sentences": [
                {
                    "begin_time": 0,
                    "end_time": 4000,
                    "text": "大家好，会议开始。",
                    "speaker_id": 0,
                },
                {
                    "begin_time": 4000,
                    "end_time": 8000,
                    "text": "好的。",
                    "speaker_id": "speaker_1",
                },
            ],
        }
    ],
}


def _disposition_value(disposition, key):
    match = re.search(rf'{key}="([^"]*)"', disposition or "")
    return match.group(1) if match else None


def _parse_multipart(content_type, body):
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    message = email.parser.BytesParser().parsebytes(raw)
    parts = []
    for part in message.get_payload():
        parts.append(
            {
                "name": _disposition_value(part.get("Content-Disposition"), "name"),
                "filename": _disposition_value(
                    part.get("Content-Disposition"), "filename"
                ),
                "data": part.get_payload(decode=True),
            }
        )
    return parts


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MockDashScope/1.0"

    def log_message(self, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state = self.server.state
        if self.path.startswith("/api/v1/uploads"):
            state["policy_requests"] += 1
            self._send_json(
                {
                    "data": {
                        "policy": "policy-value",
                        "signature": "signature-value",
                        "upload_dir": "dashscope-tmp/9",
                        "upload_host": state["base_url"] + "/oss-upload",
                        "oss_access_key_id": "ak-value",
                        "x_oss_object_acl": "default",
                        "x_oss_forbid_overwrite": "true",
                        "max_file_size_mb": "100",
                    }
                }
            )
        elif self.path == "/api/v1/tasks/task-7":
            state["polls"] += 1
            if state["polls"] < state["polls_needed"]:
                self._send_json({"output": {"task_status": "RUNNING"}})
            else:
                self._send_json(
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "subtask_status": "SUCCEEDED",
                                    "transcription_url": state["base_url"]
                                    + "/result.json",
                                }
                            ],
                        }
                    }
                )
        elif self.path == "/result.json":
            self._send_json(state["result"])
        else:
            self._send_json({"code": "NotFound"}, status=404)

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/oss-upload":
            state["uploads"] += 1
            parts = _parse_multipart(self.headers.get("Content-Type", ""), body)
            state["upload_parts"] = parts
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/api/v1/services/audio/asr/transcription":
            state["submits"] += 1
            state["submit_headers"] = {
                key.lower(): value for key, value in self.headers.items()
            }
            state["submit_body"] = json.loads(body.decode("utf-8"))
            if state.get("submit_mode") == "ambiguous":
                self._send_json({"output": {}})
            else:
                self._send_json({"output": {"task_id": "task-7"}})
        else:
            self._send_json({"code": "NotFound"}, status=404)


class MockDashScope:
    def __init__(self, result_payload=None, polls_needed=2):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.httpd.state = {
            "base_url": base,
            "polls": 0,
            "uploads": 0,
            "submits": 0,
            "policy_requests": 0,
            "polls_needed": polls_needed,
            "result": result_payload if result_payload is not None else RESULT_PAYLOAD,
        }
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return self.httpd.state["base_url"]

    @property
    def api_root(self):
        return f"{self.base}/api/v1"

    @property
    def state(self):
        return self.httpd.state

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TranscribeTestCase(unittest.TestCase):
    def setUp(self):
        self._original_connect = t._connect
        self._original_validate_api_root = t.validate_api_root
        self._original_validate_url = t.validate_aliyun_https_url
        t._connect = self._plain_connect
        t.validate_api_root = lambda value: value.rstrip("/")
        t.validate_aliyun_https_url = lambda url, label: None
        self.addCleanup(self._restore_patches)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workdir = Path(self.temporary.name)

    def _restore_patches(self):
        t._connect = self._original_connect
        t.validate_api_root = self._original_validate_api_root
        t.validate_aliyun_https_url = self._original_validate_url

    @staticmethod
    def _plain_connect(host, port, timeout):
        return http.client.HTTPConnection(host, port or 80, timeout=timeout)

    def _source_file(self, name="meeting.mp3", content=b"fake-audio-bytes"):
        path = self.workdir / "records" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class ValidationTests(TranscribeTestCase):
    def test_accepts_and_normalizes_trusted_api_roots(self):
        validate = self._original_validate_api_root
        self.assertEqual(
            validate("https://dashscope.aliyuncs.com/api/v1/"),
            "https://dashscope.aliyuncs.com/api/v1",
        )
        self.assertEqual(
            validate("https://llm-example.cn-beijing.maas.aliyuncs.com"),
            "https://llm-example.cn-beijing.maas.aliyuncs.com/api/v1",
        )
        self.assertEqual(
            validate("https://dashscope-cn-beijing.aliyuncs.com/api"),
            "https://dashscope-cn-beijing.aliyuncs.com/api/v1",
        )

    def test_rejects_untrusted_or_incompatible_api_roots(self):
        validate = self._original_validate_api_root
        for value in (
            "",
            "http://dashscope.aliyuncs.com/api/v1",
            "https://example.com/api/v1",
            "https://dashscope.aliyuncs.com.evil.test/api/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:8443/api/v1",
            "https://dashscope.aliyuncs.com/api/v1?workspace=other",
            "https://user:secret@dashscope.aliyuncs.com/api/v1",
        ):
            with self.assertRaises(t.DashScopeError, msg=value):
                validate(value)

    def test_rejects_non_aliyun_upload_targets(self):
        with self.assertRaises(t.DashScopeError):
            self._original_validate_url("https://evil.test/upload", "temporary upload URL")
        self._original_validate_url(
            "https://dashscope-file.oss-cn-beijing.aliyuncs.com/upload", "temporary upload URL"
        )


class NormalizationTests(TranscribeTestCase):
    def test_normalizes_sentences_and_speakers(self):
        result = t.normalize_result(RESULT_PAYLOAD)
        self.assertEqual(result["language"], "zh")
        self.assertEqual(result["duration_ms"], 8000)
        self.assertEqual(
            [segment["speaker"] for segment in result["segments"]],
            ["speaker_0", "speaker_1"],
        )
        self.assertEqual(result["segments"][0]["text"], "大家好，会议开始。")

    def test_falls_back_to_text_without_sentences(self):
        result = t.normalize_result(
            {
                "properties": {"original_duration_in_milliseconds": 3000},
                "transcripts": [{"transcript": "纯文本结果"}],
            }
        )
        self.assertEqual(result["text"], "纯文本结果")
        self.assertEqual(len(result["segments"]), 1)
        self.assertIsNone(result["segments"][0]["speaker"])
        self.assertEqual(result["segments"][0]["end_ms"], 3000)

    def test_rejects_results_without_text(self):
        with self.assertRaises(t.DashScopeError):
            t.normalize_result({"transcripts": [{"sentences": []}]})

    def test_renders_plain_and_timestamped_text(self):
        transcript = t.normalize_result(RESULT_PAYLOAD)
        self.assertEqual(
            t.render_text(transcript),
            "speaker_0: 大家好，会议开始。\nspeaker_1: 好的。\n",
        )
        self.assertEqual(
            t.render_text(transcript, timestamps=True),
            "[00:00:00] speaker_0: 大家好，会议开始。\n[00:00:04] speaker_1: 好的。\n",
        )


class EndToEndTests(TranscribeTestCase):
    def setUp(self):
        super().setUp()
        self.server = MockDashScope()
        self.addCleanup(self.server.close)

    def _client(self, **overrides):
        values = {"poll_schedule": [0.01]}
        values.update(overrides)
        return t.DashScopeClient("sk-test-key", self.server.api_root, **values)

    def test_transcribes_a_file_end_to_end(self):
        source = self._source_file()
        result = t.transcribe_file(
            self._client(),
            source,
            out_dir=self.workdir / "out",
            state_dir=self.workdir / "state",
            poll_schedule=[0.01],
        )
        self.assertEqual(result["status"], "transcribed")
        transcript = Path(result["transcript_path"]).read_text(encoding="utf-8")
        self.assertIn("speaker_0: 大家好，会议开始。", transcript)
        payload = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["model"], t.MODEL)
        self.assertEqual(payload["task_id"], "task-7")
        self.assertEqual(len(payload["segments"]), 2)
        self.assertEqual(list((self.workdir / "state").iterdir()), [])
        self.assertEqual(self.server.state["uploads"], 1)
        self.assertEqual(self.server.state["submits"], 1)

        body = self.server.state["submit_body"]
        self.assertEqual(body["model"], t.MODEL)
        self.assertEqual(body["input"]["file_urls"], ["oss://dashscope-tmp/9/meeting.mp3"])
        self.assertEqual(
            body["parameters"], {"channel_id": [0], "diarization_enabled": True}
        )
        headers = self.server.state["submit_headers"]
        self.assertEqual(headers["x-dashscope-async"], "enable")
        self.assertEqual(headers["x-dashscope-ossresourceresolve"], "enable")
        self.assertEqual(headers["authorization"], "Bearer sk-test-key")

        parts = self.server.state["upload_parts"]
        self.assertEqual(
            [part["name"] for part in parts],
            [
                "OSSAccessKeyId",
                "Signature",
                "policy",
                "x-oss-object-acl",
                "x-oss-forbid-overwrite",
                "key",
                "success_action_status",
                "file",
            ],
        )
        self.assertEqual(parts[-1]["filename"], "meeting.mp3")
        self.assertEqual(parts[-1]["data"], b"fake-audio-bytes")
        self.assertEqual(parts[5]["data"], b"dashscope-tmp/9/meeting.mp3")

    def test_skips_files_that_already_have_a_transcript(self):
        source = self._source_file()
        out_dir = self.workdir / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "meeting.txt").write_text("existing\n", encoding="utf-8")
        result = t.transcribe_file(
            self._client(),
            source,
            out_dir=out_dir,
            state_dir=self.workdir / "state",
            poll_schedule=[0.01],
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.server.state["policy_requests"], 0)
        self.assertEqual(self.server.state["uploads"], 0)

    def test_resumes_a_running_task_instead_of_resubmitting(self):
        source = self._source_file()
        state_dir = self.workdir / "state"
        self.server.state["polls_needed"] = 10**6
        with self.assertRaises(t.DashScopeError) as context:
            t.transcribe_file(
                self._client(),
                source,
                out_dir=self.workdir / "out",
                state_dir=state_dir,
                timeout_minutes=0.02,
                poll_schedule=[0.05],
            )
        self.assertIn("stopped waiting", str(context.exception))
        self.assertFalse((self.workdir / "out" / "meeting.txt").exists())
        states = list(state_dir.iterdir())
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "submitted")
        self.assertEqual(state["task_id"], "task-7")

        self.server.state["polls_needed"] = self.server.state["polls"] + 1
        result = t.transcribe_file(
            self._client(),
            source,
            out_dir=self.workdir / "out",
            state_dir=state_dir,
            poll_schedule=[0.01],
        )
        self.assertEqual(result["status"], "transcribed")
        self.assertEqual(self.server.state["uploads"], 1)
        self.assertEqual(self.server.state["submits"], 1)
        self.assertEqual(list(state_dir.iterdir()), [])

    def test_ambiguous_submit_is_not_retried_without_force(self):
        source = self._source_file()
        state_dir = self.workdir / "state"
        self.server.state["submit_mode"] = "ambiguous"
        with self.assertRaises(t.AmbiguousSubmitError):
            t.transcribe_file(
                self._client(),
                source,
                out_dir=self.workdir / "out",
                state_dir=state_dir,
                poll_schedule=[0.01],
            )
        states = list(state_dir.iterdir())
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "submit_unknown")

        self.server.state["submit_mode"] = None
        with self.assertRaises(t.DashScopeError) as context:
            t.transcribe_file(
                self._client(),
                source,
                out_dir=self.workdir / "out",
                state_dir=state_dir,
                poll_schedule=[0.01],
            )
        self.assertIn("--force", str(context.exception))
        self.assertEqual(self.server.state["submits"], 1)

        result = t.transcribe_file(
            self._client(),
            source,
            out_dir=self.workdir / "out",
            state_dir=state_dir,
            force=True,
            poll_schedule=[0.01],
        )
        self.assertEqual(result["status"], "transcribed")
        self.assertEqual(self.server.state["uploads"], 2)
        self.assertEqual(self.server.state["submits"], 2)

    def test_transcribes_every_supported_file_in_a_directory(self):
        self._source_file("first.mp3")
        self._source_file("second.wav")
        (self.workdir / "records" / "notes.txt").write_text("skip me", encoding="utf-8")
        client = self._client()
        files = t._supported_audio_files(self.workdir / "records")
        self.assertEqual([path.name for path in files], ["first.mp3", "second.wav"])
        for source in files:
            t.transcribe_file(
                client,
                source,
                out_dir=self.workdir / "out",
                state_dir=self.workdir / "state",
                poll_schedule=[0.01],
            )
        self.assertEqual(self.server.state["uploads"], 2)


class CliTests(TranscribeTestCase):
    def setUp(self):
        super().setUp()
        self.server = MockDashScope()
        self.addCleanup(self.server.close)
        self.config_path = self.workdir / "config" / "config.json"

    def _run(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = t.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_setup_saves_configuration_and_check_reports_it(self):
        records = self.workdir / "records"
        code, out, err = self._run(
            [
                "setup",
                "--workdir",
                str(records),
                "--api-key",
                "sk-test-1234567890",
                "--api-root",
                self.server.api_root,
                "--skip-verify",
                "--config",
                str(self.config_path),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(records.is_dir())
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["workdir"], str(records.resolve()))
        self.assertEqual(config["api_root"], self.server.api_root)

        code, out, err = self._run(["check", "--config", str(self.config_path)])
        self.assertEqual(code, 0, err)
        self.assertIn("api key: ****7890", out)
        self.assertIn("the API key is accepted", out)

    def test_setup_requires_an_api_key(self):
        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}):
            code, out, err = self._run(
                [
                    "setup",
                    "--workdir",
                    str(self.workdir / "records"),
                    "--skip-verify",
                    "--config",
                    str(self.config_path),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("API key", err)

    def test_transcribe_without_configuration_fails_clearly(self):
        code, out, err = self._run(
            ["transcribe", "--config", str(self.workdir / "missing.json")]
        )
        self.assertEqual(code, 1)
        self.assertIn("run setup first", err)

    def test_transcribe_uses_the_configured_workdir(self):
        self._source_file("meeting.mp3")
        self._run(
            [
                "setup",
                "--workdir",
                str(self.workdir / "records"),
                "--api-key",
                "sk-test-1234567890",
                "--api-root",
                self.server.api_root,
                "--skip-verify",
                "--config",
                str(self.config_path),
            ]
        )
        code, out, err = self._run(["transcribe", "--config", str(self.config_path)])
        self.assertEqual(code, 0, err)
        self.assertIn("transcript:", out)
        self.assertTrue(
            (self.workdir / "records" / "transcripts" / "meeting.txt").is_file()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
