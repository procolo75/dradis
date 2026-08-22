"""
tests/test_token_budget.py
──────────────────────────
The per-minute token budget, and what the runtime does to stay inside it.

    cd dradis && python3 -m unittest discover tests

The bug these tests pin down: the same scheduled task, reading the same page,
sometimes blew Groq's 8K ceiling and sometimes did not. That ceiling is a rolling
60-second budget counting every request a turn makes — and a turn re-sends the
whole transcript, page included, on every tool round. Whether the model took two
rounds or three was left to the provider's default temperature, so the cost of an
identical prompt varied by thousands of tokens between runs. Nothing measured
any of it: `context_window_for` existed but its only caller was a log line.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

import core                                                       # noqa: E402


class EstimateTokensTest(unittest.TestCase):
    """The estimate has one job: never say "smaller than it is"."""

    def test_scales_with_length(self):
        self.assertLess(core.estimate_tokens("short"),
                        core.estimate_tokens("short" * 100))

    def test_empty_text_costs_nothing_meaningful(self):
        self.assertLessEqual(core.estimate_tokens(""), 1)
        self.assertLessEqual(core.estimate_tokens(None), 1)

    def test_over_counts_rather_than_under(self):
        # ~4 chars/token is the optimistic real-world ratio; ours must exceed it,
        # because the whole point is to stay under a hard provider limit.
        text = "the quick brown fox jumps over the lazy dog " * 50
        self.assertGreater(core.estimate_tokens(text), len(text) / 4)

    def test_counts_tool_call_arguments_not_just_content(self):
        bare = [{"role": "assistant", "content": ""}]
        with_call = [{"role": "assistant", "content": "", "tool_calls": [
            {"function": {"arguments": '{"url": "https://example.com/a/long/path"}'}}]}]
        self.assertGreater(core.estimate_messages_tokens(with_call),
                           core.estimate_messages_tokens(bare))


class FitToolResultTest(unittest.TestCase):

    def test_short_result_is_untouched(self):
        self.assertEqual(core.fit_tool_result("hello", 1000), "hello")

    def test_long_result_is_cut_to_budget(self):
        fitted = core.fit_tool_result("x" * 40000, 500)
        self.assertLessEqual(core.estimate_tokens(fitted), 500)

    def test_truncation_is_announced(self):
        # A model handed a silently halved page will summarise the half as if it
        # were the whole thing. Saying so is what makes the answer partial rather
        # than wrong.
        self.assertIn("truncated", core.fit_tool_result("x" * 40000, 500))
        self.assertNotIn("truncated", core.fit_tool_result("short", 500))

    def test_zero_budget_yields_only_the_marker(self):
        self.assertNotIn("x", core.fit_tool_result("x" * 100, 0))


class TrimToWindowTest(unittest.TestCase):

    def _conversation(self, newest="new page "):
        return [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "the question"},
            {"role": "tool", "tool_call_id": "1", "content": "old page " * 4000},
            {"role": "tool", "tool_call_id": "2", "content": newest * 100},
        ]

    def test_leaves_a_fitting_conversation_alone(self):
        msgs = [{"role": "system", "content": "rules"},
                {"role": "user", "content": "hi"}]
        core._trim_to_window(msgs, 8192, 2048)
        self.assertEqual(msgs[1]["content"], "hi")

    def test_drops_older_results_until_it_fits(self):
        msgs = self._conversation()
        core._trim_to_window(msgs, 8192, 2048)
        self.assertLessEqual(core.estimate_messages_tokens(msgs),
                             8192 - 2048 - core._BUDGET_HEADROOM)

    def test_never_touches_the_system_prompt_or_the_question(self):
        msgs = self._conversation()
        core._trim_to_window(msgs, 8192, 2048)
        self.assertEqual(msgs[0]["content"], "rules")
        self.assertEqual(msgs[1]["content"], "the question")

    def test_sacrifices_the_oldest_result_first(self):
        msgs = self._conversation()
        core._trim_to_window(msgs, 8192, 2048)
        self.assertNotIn("old page", msgs[2]["content"])

    def test_spares_the_newest_result(self):
        # It is what the model is about to reason over, and when the newest tool
        # is the one that failed it carries the reason for the failure.
        msgs = self._conversation()
        core._trim_to_window(msgs, 8192, 2048)
        self.assertIn("new page", msgs[3]["content"])

    def test_an_oversized_newest_result_is_kept_and_the_provider_decides(self):
        # Nothing left to give: rather than silently gut the round that just
        # fetched, the request goes as it is.
        msgs = self._conversation(newest="huge page " * 400)
        core._trim_to_window(msgs, 8192, 2048)
        self.assertIn("huge page", msgs[3]["content"])
        self.assertNotIn("old page", msgs[2]["content"])


class TpmBudgetTest(unittest.TestCase):
    """Time is injected: a test that actually waited a minute would not be run."""

    def setUp(self):
        core.reset_tpm()

    def tearDown(self):
        core.reset_tpm()

    def test_uncapped_provider_never_waits(self):
        core.record_tpm("openrouter", 500000, now=1000.0)
        self.assertEqual(core.tpm_wait_seconds("openrouter", 500000, now=1000.0), 0.0)

    def test_spend_is_counted(self):
        core.record_tpm("groq", 3000, now=1000.0)
        self.assertEqual(core._tpm_used("groq", 1000.0), 3000)

    def test_request_that_fits_does_not_wait(self):
        core.record_tpm("groq", 3000, now=1000.0)
        self.assertEqual(core.tpm_wait_seconds("groq", 4000, now=1000.0), 0.0)

    def test_request_that_does_not_fit_waits_for_the_window_to_roll(self):
        core.record_tpm("groq", 7000, now=1000.0)
        wait = core.tpm_wait_seconds("groq", 4000, now=1010.0)
        # The 7000 was spent 10s ago, so it ages out 50s from now.
        self.assertAlmostEqual(wait, 50.1, places=1)

    def test_spend_older_than_a_minute_is_forgotten(self):
        core.record_tpm("groq", 8000, now=1000.0)
        self.assertEqual(core._tpm_used("groq", 1061.0), 0)
        self.assertEqual(core.tpm_wait_seconds("groq", 8000, now=1061.0), 0.0)

    def test_waits_only_for_the_oldest_spend_it_needs(self):
        # Three separate spends: freeing the first is enough, so the wait is tied
        # to that one and not to the newest.
        core.record_tpm("groq", 3000, now=1000.0)
        core.record_tpm("groq", 2000, now=1020.0)
        core.record_tpm("groq", 2000, now=1040.0)
        wait = core.tpm_wait_seconds("groq", 3000, now=1050.0)
        self.assertAlmostEqual(wait, 10.1, places=1)   # first spend ages out at 1060

    def test_buckets_are_per_provider(self):
        core.record_tpm("groq", 8000, now=1000.0)
        self.assertGreater(core.tpm_wait_seconds("groq", 1000, now=1000.0), 0)
        self.assertEqual(core.tpm_wait_seconds("openai", 1000, now=1000.0), 0.0)


class CeilingTest(unittest.TestCase):
    """The largest a single request may be."""

    def test_a_small_window_wins_over_a_large_budget(self):
        self.assertEqual(core.ceiling_for("llama-3.3-70b", "groq"), 8000)

    def test_a_small_budget_wins_over_a_large_window(self):
        # gpt-oss-120b has room for far more than Groq will let it spend.
        self.assertEqual(core.ceiling_for("gpt-4o", "groq"), 8000)

    def test_an_uncapped_provider_gets_the_whole_window(self):
        self.assertEqual(core.ceiling_for("gemini-2.0-flash", "gemini"), 1000000)

    def test_it_does_not_move_with_what_the_minute_has_left(self):
        # The point of the ceiling: the same page must trim the same way whether
        # the minute is fresh or nearly spent, or the answer changes run to run.
        core.reset_tpm()
        before = core.ceiling_for("gpt-4o", "groq")
        core.record_tpm("groq", 7500)
        self.assertEqual(core.ceiling_for("gpt-4o", "groq"), before)
        core.reset_tpm()


class RetryAfterTest(unittest.TestCase):

    class _Resp:
        def __init__(self, status_code=429, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

    class _Err(Exception):
        def __init__(self, msg, response=None, status_code=None):
            super().__init__(msg)
            self.response = response
            self.status_code = status_code

    def test_ordinary_error_is_not_a_rate_limit(self):
        self.assertIsNone(core.retry_after_seconds(ValueError("bad json")))

    def test_reads_the_retry_after_header(self):
        err = self._Err("rate_limit_exceeded", response=self._Resp(headers={"retry-after": "3"}))
        self.assertEqual(core.retry_after_seconds(err), 3.0)

    def test_reads_the_figure_groq_puts_in_the_message(self):
        err = self._Err(
            "Error code: 429 - rate_limit_exceeded: Limit 8000, Used 3704, "
            "Requested 4502. Please try again in 1.52s.",
            response=self._Resp())
        self.assertAlmostEqual(core.retry_after_seconds(err), 1.52)

    def test_header_wins_over_the_message(self):
        err = self._Err("rate_limit_exceeded, try again in 9.0s",
                        response=self._Resp(headers={"retry-after": "2"}))
        self.assertEqual(core.retry_after_seconds(err), 2.0)

    def test_rate_limit_without_a_figure_still_waits(self):
        err = self._Err("429 Too Many Requests", response=self._Resp())
        self.assertGreater(core.retry_after_seconds(err), 0)


if __name__ == "__main__":
    unittest.main()
