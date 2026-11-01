import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from scripts.token_activity.collect import (
    CollectionError,
    MAX_TOKEN_COUNT,
    collect_usage,
    normalize_usage,
    resolve_binary,
)


@contextlib.contextmanager
def fake_cli(body):
    """A disposable local protocol peer; it never contacts an account or network."""
    with tempfile.TemporaryDirectory(prefix="token-activity-test-") as temporary:
        executable = Path(temporary) / "codex"
        preamble = """
import json, os, sys, time
from pathlib import Path
record = Path(__file__).with_suffix('.requests')
Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))
def receive():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    message = json.loads(line)
    with record.open('a') as output:
        output.write(json.dumps(message) + '\\n')
    return message
def send(value):
    print(json.dumps(value), flush=True)
def handshake():
    assert receive()['method'] == 'initialize'
    send({'id': 1, 'result': {'userAgent': 'synthetic'}})
    assert receive() == {'method': 'initialized'}
    assert receive() == {'id': 2, 'method': 'account/usage/read'}
"""
        executable.write_text("#!" + sys.executable + "\n" + textwrap.dedent(preamble) + textwrap.dedent(body))
        executable.chmod(0o700)
        yield executable


def recorded(executable):
    path = executable.with_suffix(".requests")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class UsageValidationTests(unittest.TestCase):
    def test_only_sorted_aggregate_buckets_survive(self):
        normalized = normalize_usage({
            "summary": {"lifetimeTokens": None, "accountId": "synthetic-private-id"},
            "dailyUsageBuckets": [
                {"startDate": "2026-11-01", "tokens": 20, "email": "synthetic@example.invalid"},
                {"startDate": "2026-10-31", "tokens": 0},
            ],
            "threadUsage": {"threadId": "synthetic-private-thread"},
        })
        self.assertEqual(set(normalized), {"source", "fetched_at", "daily"})
        self.assertEqual(normalized["source"], "codex-account-usage")
        self.assertRegex(normalized["fetched_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(normalized["daily"], [{"date": "2026-10-31", "tokens": 0}, {"date": "2026-11-01", "tokens": 20}])
        self.assertNotIn("synthetic", json.dumps(normalized))

    def test_unavailable_is_not_empty_or_zero(self):
        for response in ({"summary": {}}, {"summary": {}, "dailyUsageBuckets": None}):
            with self.subTest(response=response), self.assertRaisesRegex(CollectionError, "unavailable"):
                normalize_usage(response)
        self.assertEqual(normalize_usage({"summary": {}, "dailyUsageBuckets": []})["daily"], [])

    def test_duplicate_dates_fail_without_summing(self):
        bucket = {"startDate": "2026-11-01", "tokens": 5}
        with self.assertRaisesRegex(CollectionError, "duplicate"):
            normalize_usage({"summary": {}, "dailyUsageBuckets": [bucket, bucket]})

    def test_invalid_dates_and_counts_fail(self):
        for invalid in ("2026-02-30", "2026-1-01", "2026-11-01T00:00:00Z", "0000-01-01", "2026-11-01\n", 20261101):
            with self.subTest(date=invalid), self.assertRaises(CollectionError):
                normalize_usage({"summary": {}, "dailyUsageBuckets": [{"startDate": invalid, "tokens": 1}]})
        for invalid in (True, False, -1, 1.5, "100", None, MAX_TOKEN_COUNT + 1, float("nan")):
            with self.subTest(count=repr(invalid)), self.assertRaises(CollectionError):
                normalize_usage({"summary": {}, "dailyUsageBuckets": [{"startDate": "2026-11-01", "tokens": invalid}]})
        self.assertEqual(normalize_usage({"summary": {}, "dailyUsageBuckets": [{"startDate": "2024-02-29", "tokens": MAX_TOKEN_COUNT}]})["daily"][0]["tokens"], MAX_TOKEN_COUNT)

    def test_malformed_structure_and_excess_bucket_count_fail(self):
        for invalid in (None, [], {}, {"summary": None, "dailyUsageBuckets": []},
                        {"summary": {}, "dailyUsageBuckets": {}}, {"summary": {}, "dailyUsageBuckets": [None]}):
            with self.subTest(response=invalid), self.assertRaises(CollectionError):
                normalize_usage(invalid)
        with patch("scripts.token_activity.collect.MAX_DAILY_BUCKETS", 1), self.assertRaises(CollectionError):
            normalize_usage({"summary": {}, "dailyUsageBuckets": [
                {"startDate": "2026-11-01", "tokens": 1}, {"startDate": "2026-10-31", "tokens": 1}]})


class CollectorProtocolTests(unittest.TestCase):
    def test_allowlisted_handshake_drops_private_fields_and_child_output(self):
        with fake_cli("""
            handshake()
            print('synthetic-private-stderr', file=sys.stderr, flush=True)
            send({'method': 'account/updated', 'params': {'email': 'synthetic-private-email'}})
            send({'id': 2, 'result': {'summary': {'lifetimeTokens': 999},
                'dailyUsageBuckets': [{'startDate': '2026-11-01', 'tokens': 123}],
                'threadUsage': {'threadId': 'synthetic-private-thread'}}})
            time.sleep(10)
        """) as executable:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = collect_usage(str(executable), timeout=3)
            self.assertEqual(result["daily"], [{"date": "2026-11-01", "tokens": 123}])
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            requests = recorded(executable)
            self.assertEqual([request["method"] for request in requests], ["initialize", "initialized", "account/usage/read"])
            self.assertFalse(requests[0]["params"]["capabilities"]["experimentalApi"])
            self.assertNotIn("params", requests[2])
            with self.assertRaises(ProcessLookupError):
                os.kill(int(executable.with_suffix(".pid").read_text()), 0)

    def test_chunked_jsonl_and_blank_lines(self):
        with fake_cli("""
            handshake()
            raw = (json.dumps({'id': 2, 'result': {'summary': {}, 'dailyUsageBuckets': []}}) + '\\n').encode()
            sys.stdout.buffer.write(b'\\n' + raw[:17]); sys.stdout.buffer.flush()
            time.sleep(0.02)
            sys.stdout.buffer.write(raw[17:]); sys.stdout.buffer.flush()
        """) as executable:
            self.assertEqual(collect_usage(str(executable), timeout=3)["daily"], [])

    def test_notification_batch_does_not_hide_usage_reply(self):
        with fake_cli("""
            handshake()
            noise = json.dumps({'method': 'status/changed', 'params': {'private': 'synthetic'}}) + '\\n'
            sys.stdout.write(noise * 2000)
            send({'id': 2, 'result': {'summary': {}, 'dailyUsageBuckets': []}})
        """) as executable:
            self.assertEqual(collect_usage(str(executable), timeout=3)["daily"], [])

    def test_server_authentication_request_is_not_answered(self):
        with fake_cli("""
            receive()
            send({'id': 99, 'method': 'account/chatgptAuthTokens/refresh', 'params': {'reason': 'unauthorized'}})
            receive()
        """) as executable:
            with self.assertRaisesRegex(CollectionError, "unsupported interactive operation"):
                collect_usage(str(executable), timeout=3)
            self.assertEqual([request["method"] for request in recorded(executable)], ["initialize"])

    def test_error_response_never_exposes_raw_message(self):
        with fake_cli("""
            handshake()
            send({'id': 2, 'error': {'code': -1, 'message': 'synthetic-private-account-detail'}})
        """) as executable:
            with self.assertRaises(CollectionError) as failure:
                collect_usage(str(executable), timeout=3)
            self.assertNotIn("synthetic-private", str(failure.exception))

    def test_timeout_closes_child_and_never_requests_usage(self):
        with fake_cli("""
            receive()
            time.sleep(10)
        """) as executable:
            started = time.monotonic()
            with self.assertRaisesRegex(CollectionError, "timed out"):
                collect_usage(str(executable), timeout=0.15)
            self.assertLess(time.monotonic() - started, 2)
            # Slow interpreter startup may consume the whole deadline before
            # the fake peer can record initialization; neither path may read usage.
            self.assertIn([request["method"] for request in recorded(executable)], [[], ["initialize"]])

    def test_eof_invalid_json_unexpected_response_and_null_result_fail(self):
        bodies = ["receive()", "receive(); print('not-json', flush=True)",
                  "receive(); print('[' * 2000 + ']' * 2000, flush=True)",
                  "receive(); send({'id': True, 'result': {}})",
                  "receive(); send({'id': 2, 'result': {}})",
                  "receive(); send({'id': 1, 'result': None})"]
        for body in bodies:
            with self.subTest(body=body), fake_cli(body) as executable, self.assertRaises(CollectionError):
                collect_usage(str(executable), timeout=3)

    def test_response_limit_includes_unframed_stream(self):
        with fake_cli("""
            receive()
            sys.stdout.write('x' * 2048); sys.stdout.flush()
            time.sleep(10)
        """) as executable:
            with patch("scripts.token_activity.collect.MAX_RESPONSE_BYTES", 1024), self.assertRaisesRegex(CollectionError, "size limit"):
                collect_usage(str(executable), timeout=3)

    def test_invalid_timeout_rejected_before_launch(self):
        for invalid in (True, 0, -1, float("nan"), float("inf"), 301, "25"):
            with self.subTest(timeout=repr(invalid)), patch("scripts.token_activity.collect.subprocess.Popen") as launch:
                with self.assertRaises(CollectionError):
                    collect_usage(timeout=invalid)
                launch.assert_not_called()


class BinaryResolutionTests(unittest.TestCase):
    def test_explicit_binary_must_be_executable_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codex"
            path.write_text("fixture")
            with self.assertRaises(CollectionError):
                resolve_binary(str(path))
            path.chmod(0o700)
            self.assertEqual(resolve_binary(str(path)), str(path.resolve()))
            with self.assertRaises(CollectionError):
                resolve_binary(temporary)

    def test_path_binary_has_precedence(self):
        with fake_cli("pass") as executable, patch("scripts.token_activity.collect.shutil.which", return_value=str(executable)):
            self.assertEqual(resolve_binary(), str(executable.resolve()))

    def test_extension_fallback_uses_newest_numeric_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for version in ("9.1", "10.1"):
                path = home / f".vscode/extensions/openai.chatgpt-{version}/bin/macos-aarch64/codex"
                path.parent.mkdir(parents=True)
                path.write_text("fixture"); path.chmod(0o700)
            with patch("scripts.token_activity.collect.Path.home", return_value=home), \
                 patch("scripts.token_activity.collect.shutil.which", return_value=None), \
                 patch("scripts.token_activity.collect.os.uname", return_value=type("System", (), {"machine": "arm64"})()), \
                 patch("scripts.token_activity.collect._executable", side_effect=lambda path: str(path).startswith(str(home)) and path.is_file()):
                self.assertIn("openai.chatgpt-10.1", resolve_binary())


if __name__ == "__main__":
    unittest.main()
