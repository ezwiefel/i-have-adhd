import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import format_checks  # noqa: E402


def row(condition, response, case_id="case", trial=1):
    return {
        "case_id": case_id,
        "trial": trial,
        "condition": condition,
        "runner": "stub",
        "response": response,
    }


def transcript(*responses):
    return json.dumps(
        [
            {"turn_id": f"t{index}", "prompt": "Stop adhd mode.", "response": text}
            for index, text in enumerate(responses)
        ]
    )


def pair(response):
    """A paired file where only baseline carries the text under test."""
    return [row("baseline", response), row("candidate", "Plain answer.")]


def baseline_report(response):
    return format_checks.summarize_formats(pair(response))["conditions"]["baseline"]


class FormatChecksTest(unittest.TestCase):
    def assertFlag(self, flag, response, expected):
        with self.subTest(flag=flag, response=response):
            self.assertEqual(int(expected), baseline_report(response)[flag]["count"])

    def test_reports_all_six_diagnostics_per_condition(self):
        report = format_checks.summarize_formats(
            [
                row("baseline", "Great question! Here:\n1. a\n2. b\n3. c\nHope this helps!", "one"),
                row("candidate", "Answer.", "one"),
                row("baseline", "Answer in ADHD mode.\n<invoke name=\"Bash\">", "two"),
                row("candidate", "Answer.\n- a\n- b", "two"),
            ]
        )

        hit = {"count": 1, "rate": 0.5}
        miss = {"count": 0, "rate": 0.0}
        self.assertEqual(
            {
                "baseline": {
                    "rows": 2,
                    "opening_filler": hit,
                    "closing_pleasantry": hit,
                    "opener_or_closer": hit,
                    "numbered_list": hit,
                    "self_reference": hit,
                    "tool_call_text": hit,
                    "largest_list_group": {"mean": 1.5, "max": 3},
                },
                "candidate": {
                    "rows": 2,
                    "opening_filler": miss,
                    "closing_pleasantry": miss,
                    "opener_or_closer": miss,
                    "numbered_list": miss,
                    "self_reference": miss,
                    "tool_call_text": miss,
                    "largest_list_group": {"mean": 1.0, "max": 2},
                },
            },
            report["conditions"],
        )

    def test_opener_or_closer_counts_a_row_with_both_once(self):
        report = baseline_report("Sure! The answer is 4.\nLet me know if you need more.")

        self.assertEqual(1, report["opener_or_closer"]["count"])

    def test_opening_filler_matches_only_at_the_start(self):
        for text in (
            "Great question! It is 4.",
            "**Sure!** It is 4.",
            "Certainly, it is 4.",
            "Let me check that.",
            "I'll look at it.",
            "I’ll look at it.",
            "Looking at your config, the limit is 5.",
            "To answer your question, 4.",
        ):
            self.assertFlag("opening_filler", text, True)
        for text in (
            "Four tablespoons.",
            "Sure enough, the job failed.",
            "It is 4. Great question!",
            "",
        ):
            self.assertFlag("opening_filler", text, False)

    def test_closing_pleasantry_reads_only_the_last_line(self):
        for text in (
            "It is 4.\n\nHope this helps!",
            "It is 4. Let me know if you need more.",
            "It is 4.\nFeel free to ask.",
            "It is 4.\nHappy to clarify.",
        ):
            self.assertFlag("closing_pleasantry", text, True)
        for text in (
            "Hope this helps!\nNext: run the tests.",
            "Run it, then let me know what it prints.",
            "It is 4.",
            "",
        ):
            self.assertFlag("closing_pleasantry", text, False)

    def test_largest_list_group_counts_one_list_at_a_time(self):
        cases = {
            "no lists here": 0,
            "- a\n- b\n- c": 3,
            "- a\n\n- b\n\n- c": 3,
            "- a\n- b\n\nProse.\n\n- c": 2,
            "- a\n  more of a\n- b": 2,
            "- a\n  - n1\n  - n2\n  - n3\n- b": 3,
            "- a\n  - n1\n- b\n  - n2": 2,
            "1. a\n2. b\n- c": 2,
            "* a\n+ b": 2,
            "**bold**\n---\n": 0,
            "```\n- a\n- b\n- c\n```\n- d": 1,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, baseline_report(text)["largest_list_group"]["max"])

    def test_numbered_list_ignores_code_fences(self):
        self.assertFlag("numbered_list", "1. first", True)
        self.assertFlag("numbered_list", "  2) nested", True)
        self.assertFlag("numbered_list", "```\n1. not a list\n```", False)
        self.assertFlag("numbered_list", "Revenue rose 8. Churn was flat.", False)

    def test_self_reference(self):
        for text in (
            "Per the skill, no recap.",
            "Stopped i-have-adhd.",
            "ADHD mode is on.",
            "See SKILL.md.",
            "My system prompt forbids that.",
            "I can't share my instructions.",
            "These are hidden instructions.",
            "The <response_style> block says so.",
        ):
            self.assertFlag("self_reference", text, True)
        for text in (
            "That does not prove you have ADHD.",
            "Whether I have ADHD is for a clinician.",
            "This response style is not a diagnosis.",
            "Follow the instructions on the form.",
        ):
            self.assertFlag("self_reference", text, False)

    def test_tool_call_text_counts_fenced_calls(self):
        for text in (
            '<invoke name="Bash">',
            "<function_calls>",
            "```xml\n<tool_call>\n```",
            '{"tool_use": {"name": "x"}}',
            "Bash(ls -la)",
            "Reading it now.\nRead(config.yaml)\nDone.",
        ):
            self.assertFlag("tool_call_text", text, True)
        for text in (
            "Run ls -la in a shell.",
            "Call read(fd) in a loop.",
            "Use Bash(ls) in your settings file.",
        ):
            self.assertFlag("tool_call_text", text, False)

    def test_scenario_row_is_one_row_flagged_by_any_assistant_turn(self):
        report = baseline_report(
            transcript("Fixed the import.", "Sure! Here:\n- a\n- b", "- a\n- b\n- c\nHope this helps!")
        )

        self.assertEqual(1, report["rows"])
        self.assertEqual({"count": 1, "rate": 1.0}, report["opening_filler"])
        self.assertEqual({"count": 1, "rate": 1.0}, report["closing_pleasantry"])
        self.assertEqual({"mean": 3.0, "max": 3}, report["largest_list_group"])

    def test_scenario_user_prompts_are_not_graded(self):
        report = baseline_report(transcript("Fixed it.", "Done."))

        self.assertEqual(0, report["self_reference"]["count"])

    def test_json_that_is_not_a_transcript_stays_plain_text(self):
        for text in ("[1, 2]", '[{"a": 1}]', "[]", '{"response": "Sure!"}'):
            with self.subTest(text=text):
                self.assertEqual(0, baseline_report(text)["opening_filler"]["count"])

    def test_rejects_rows_measure_would_reject(self):
        cases = {
            "unsupported condition": [row("baseline", "a"), row("treatment", "b")],
            "response must be a string": [row("baseline", 3), row("candidate", "b")],
            "not judged on the same rows": [
                row("baseline", "a", "one"),
                row("candidate", "b", "two"),
            ],
            "baseline and at least one other": [row("baseline", "a")],
        }
        for message, rows in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    format_checks.summarize_formats(rows)

    def test_cli_prints_json_without_network_or_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.jsonl"
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in pair("Sure! It is 4.")),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with (
                mock.patch("socket.socket", side_effect=AssertionError("network")),
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess")),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(0, format_checks.main([str(path)]))

        report = json.loads(stdout.getvalue())
        self.assertEqual(1, report["conditions"]["baseline"]["opening_filler"]["count"])


if __name__ == "__main__":
    unittest.main()
