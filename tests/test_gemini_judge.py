import contextlib
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "runners"))

import gemini_judge  # noqa: E402
import run_evals  # noqa: E402


KEY = "test-key-never-printed"
SUCCESS = {
    "candidates": [{"content": {"parts": [{"text": '{"A": 1}'}]}, "finishReason": "STOP"}],
    "usageMetadata": {
        "promptTokenCount": 11,
        "candidatesTokenCount": 7,
        "thoughtsTokenCount": 5,
    },
}


def http_error(code, headers=None):
    return urllib.error.HTTPError(
        gemini_judge.URL, code, "fixture reason", headers or {}, io.BytesIO(b"")
    )


class FakeTime:
    """Stands in for the time module so retry waits never really sleep."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class GeminiJudgeTest(unittest.TestCase):
    def run_runner(self, *outcomes, env=None):
        """Run main() against scripted urlopen outcomes, with no network."""
        clock = FakeTime()
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {"GEMINI_API_KEY": KEY} if env is None else env
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(gemini_judge.urllib.request, "urlopen", side_effect=outcomes) as urlopen,
            mock.patch.object(gemini_judge, "time", clock),
            mock.patch.object(sys, "stdin", io.StringIO("grade this")),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = gemini_judge.main()
        self.assertNotIn(KEY, stderr.getvalue())
        self.assertNotIn(KEY, stdout.getvalue())
        return code, stdout.getvalue(), stderr.getvalue(), urlopen, clock

    @staticmethod
    def body(payload):
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_success_prints_claude_json_with_null_cost(self):
        code, stdout, _, urlopen, clock = self.run_runner(self.body(SUCCESS))

        self.assertEqual(0, code)
        self.assertEqual([], clock.sleeps)
        # The harness's own parser is the real consumer of this output.
        payload = json.loads(stdout)
        self.assertIn("total_cost_usd", payload)
        text, usage, cost = run_evals._parse_response(stdout, "claude-json")
        self.assertEqual('{"A": 1}', text)
        self.assertIsNone(cost)
        self.assertFalse(run_evals.failed_generation(payload))
        # Thinking tokens bill as output, so they count as output here.
        self.assertEqual((11, 12), run_evals._usage_tokens(usage))

    def test_request_matches_the_documented_shape(self):
        _, _, _, urlopen, _ = self.run_runner(self.body(SUCCESS))

        request = urlopen.call_args.args[0]
        self.assertEqual(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.8-flash:generateContent",
            request.full_url,
        )
        self.assertEqual("POST", request.get_method())
        self.assertEqual(KEY, request.get_header("X-goog-api-key"))
        self.assertEqual(
            {
                "contents": [{"parts": [{"text": "grade this"}]}],
                "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}},
            },
            json.loads(request.data),
        )

    def test_thought_parts_are_left_out_of_the_result(self):
        payload = {
            "candidates": [
                {"content": {"parts": [{"text": "reasoning", "thought": True}, {"text": "answer"}]}}
            ]
        }

        code, stdout, _, _, _ = self.run_runner(self.body(payload))

        self.assertEqual(0, code)
        self.assertEqual("answer", json.loads(stdout)["result"])

    def test_429_waits_for_retry_after_then_succeeds(self):
        code, stdout, _, urlopen, clock = self.run_runner(
            http_error(429, {"Retry-After": "7"}), self.body(SUCCESS)
        )

        self.assertEqual(0, code)
        self.assertEqual([7.0], clock.sleeps)
        self.assertEqual(2, urlopen.call_count)
        self.assertEqual('{"A": 1}', json.loads(stdout)["result"])

    def test_429_without_usable_retry_after_backs_off(self):
        for value in (None, "Wed, 21 Oct 2026 07:28:00 GMT", "0", "nan", "-3"):
            with self.subTest(value=value):
                headers = {} if value is None else {"Retry-After": value}
                code, _, _, _, clock = self.run_runner(
                    http_error(429, headers), http_error(429, headers), self.body(SUCCESS)
                )

                self.assertEqual(0, code)
                self.assertEqual([1, 2], clock.sleeps)

    def test_429_until_exhausted_gives_up_after_about_two_minutes(self):
        code, stdout, stderr, urlopen, clock = self.run_runner(
            *[http_error(429) for _ in range(50)]
        )

        self.assertNotEqual(0, code)
        self.assertEqual("", stdout)
        self.assertIn("429", stderr)
        self.assertGreater(urlopen.call_count, 1)
        self.assertLessEqual(clock.now, 120)
        self.assertGreater(clock.now, 60)

    def test_retry_after_beyond_the_window_gives_up_without_waiting(self):
        code, _, stderr, urlopen, clock = self.run_runner(http_error(429, {"Retry-After": "3600"}))

        self.assertNotEqual(0, code)
        self.assertEqual([], clock.sleeps)
        self.assertEqual(1, urlopen.call_count)
        self.assertIn("429", stderr)

    def test_other_http_errors_exit_nonzero_without_retrying(self):
        for status in (400, 403, 500):
            with self.subTest(status=status):
                code, stdout, stderr, urlopen, clock = self.run_runner(http_error(status))

                self.assertNotEqual(0, code)
                self.assertEqual("", stdout)
                self.assertIn(str(status), stderr)
                self.assertEqual(1, urlopen.call_count)
                self.assertEqual([], clock.sleeps)

    def test_network_failure_exits_nonzero(self):
        for failure in (urllib.error.URLError("no route"), TimeoutError("timed out")):
            with self.subTest(failure=failure):
                code, stdout, stderr, _, _ = self.run_runner(failure)

                self.assertNotEqual(0, code)
                self.assertEqual("", stdout)
                self.assertTrue(stderr.strip())

    def test_malformed_response_exits_nonzero(self):
        bodies = {
            "not json": b"<html>gateway</html>",
            "not an object": b"[1, 2]",
            "no candidates": b'{"promptFeedback": {"blockReason": "SAFETY"}}',
            "empty candidates": b'{"candidates": []}',
            "no content": b'{"candidates": [{"finishReason": "SAFETY"}]}',
            "blank text": b'{"candidates": [{"content": {"parts": [{"text": "  "}]}}]}',
            "only thoughts": b'{"candidates": [{"content": {"parts": [{"text": "x", "thought": true}]}}]}',
            "non-string text": b'{"candidates": [{"content": {"parts": [{"text": 3}]}}]}',
        }
        for name, body in bodies.items():
            with self.subTest(name):
                code, stdout, stderr, _, _ = self.run_runner(io.BytesIO(body))

                self.assertNotEqual(0, code)
                self.assertEqual("", stdout)
                self.assertTrue(stderr.strip())

    def test_missing_key_exits_nonzero_without_calling_the_api(self):
        for env in ({}, {"GEMINI_API_KEY": "   "}):
            with self.subTest(env=env):
                code, stdout, stderr, urlopen, _ = self.run_runner(env=env)

                self.assertNotEqual(0, code)
                self.assertEqual("", stdout)
                self.assertIn("GEMINI_API_KEY", stderr)
                urlopen.assert_not_called()

    def test_key_unusable_in_a_header_is_refused_without_echoing_it(self):
        # http.client's "Invalid header value" error quotes the value verbatim.
        code, _, stderr, urlopen, _ = self.run_runner(env={"GEMINI_API_KEY": f"{KEY}\nX: y"})

        self.assertNotEqual(0, code)
        self.assertIn("GEMINI_API_KEY", stderr)
        urlopen.assert_not_called()

    def test_runner_is_registered_for_the_judge(self):
        config = json.loads((ROOT / "evals" / "runners.example.json").read_text(encoding="utf-8"))
        runner = config["gemini-3-8-flash"]

        self.assertEqual("claude-json", runner["response_format"])
        self.assertEqual(["python3", "{root}/scripts/runners/gemini_judge.py"], runner["command"])


if __name__ == "__main__":
    unittest.main()
