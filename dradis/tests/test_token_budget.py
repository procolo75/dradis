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

v4.5.1 adds what that release still got wrong. The estimate was calibrated for
prose and the pages it had to price were three-quarters URL, so a request
estimated at ~4 300 tokens was billed at ~6 033 and refused for being larger
than the whole minute. The tool schemas were never counted at all. And the
window table said gpt-oss held 8 192 tokens, which is Groq's free minute rather
than the model's 131 072 — so paying for a bigger plan would have changed
nothing.
"""

import sys
import unittest
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

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


class PageCleaningTest(unittest.TestCase):
    """What a fetched page is worth, decided before a token is spent on it.

    The fixtures are one URL read twice in the same minute: the school news
    archive as Jina's readability pass returns it, and the same page as text.
    The first has none of the eighteen headlines that are in the HTML.
    """

    @classmethod
    def setUpClass(cls):
        cls.readability = (_FIXTURES / "jina_school_news_archive.md").read_text(encoding="utf-8")
        cls.text_mode   = (_FIXTURES / "jina_school_news_archive.text.md").read_text(encoding="utf-8")

    def test_image_tags_are_dropped(self):
        self.assertNotIn("![", core.clean_page("hello ![alt](https://x/y.png) world"))
        self.assertIn("hello", core.clean_page("hello ![alt](https://x/y.png) world"))

    def test_cleaning_takes_nearly_half_of_the_school_archive(self):
        # 11 615 characters in, 6 274 out, and not one word of news lost —
        # the difference is eighteen signed thumbnail URLs.
        cleaned = core.clean_page(self.readability)
        self.assertLess(len(cleaned), len(self.readability) * 0.6)

    def test_prose_keeps_link_labels_and_drops_their_targets(self):
        text = "[Gennaio](https://example.com/archivio-news?date=2026-01)"
        self.assertEqual(core.prose_chars(text), len("Gennaio"))

    def test_a_page_of_addresses_scores_below_the_threshold(self):
        cleaned = core.clean_page(self.readability)
        share   = core.prose_chars(cleaned) / len(cleaned)
        self.assertLess(share, core.MIN_PROSE_SHARE)

    def test_the_same_page_read_as_text_scores_above_it(self):
        cleaned = core.clean_page(self.text_mode)
        share   = core.prose_chars(cleaned) / len(cleaned)
        self.assertGreater(share, core.MIN_PROSE_SHARE)

    def test_the_text_reading_carries_the_headlines_the_other_lost(self):
        # The whole reason the second read is worth an HTTP request.
        for headline in ("CHIUSURA SCUOLA DEL 14/08/2026",
                         "Ingresso posticipato del 25-06-2026"):
            self.assertNotIn(headline.upper(), self.readability.upper())
            self.assertIn(headline.upper(), self.text_mode.upper())

    def test_an_ordinary_article_is_not_mistaken_for_navigation(self):
        article = ("Il consiglio si è riunito ieri sera per discutere il bilancio, "
                   "che secondo [il documento](https://example.com/doc) chiude in "
                   "pareggio. La seduta è durata tre ore e si è conclusa con un "
                   "voto unanime dei presenti in aula consiliare. ") * 4
        share = core.prose_chars(article) / len(article)
        self.assertGreater(share, core.MIN_PROSE_SHARE)


class SchemaEstimateTest(unittest.TestCase):
    """Tool definitions are prompt tokens. Nothing counted them."""

    schemas = [{"type": "function", "function": {
        "name": "read_url", "description": "Fetch and return the text of a page.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}}]

    def test_no_schemas_cost_nothing(self):
        self.assertEqual(core.estimate_schema_tokens(None), 0)
        self.assertEqual(core.estimate_schema_tokens([]), 0)

    def test_schemas_cost_something(self):
        self.assertGreater(core.estimate_schema_tokens(self.schemas), 20)

    def test_a_request_is_priced_with_them(self):
        messages = [{"role": "user", "content": "hello"}]
        self.assertGreater(core.estimate_request_tokens(messages, self.schemas),
                           core.estimate_request_tokens(messages, None))


class CalibrationTest(unittest.TestCase):
    """The provider's own usage figures, fed back into the estimate."""

    def setUp(self):
        core.reset_calibration()

    def tearDown(self):
        core.reset_calibration()

    def test_an_estimate_that_over_counts_is_left_alone(self):
        core.calibrate("groq", estimated=1000, actual=800)
        self.assertEqual(core.correction_for("groq"), 1.0)

    def test_under_counting_raises_later_estimates(self):
        messages = [{"role": "user", "content": "x" * 2200}]
        before = core.estimate_request_tokens(messages, None, "groq")
        core.calibrate("groq", estimated=1000, actual=1400)
        self.assertGreater(core.estimate_request_tokens(messages, None, "groq"), before)

    def test_the_worst_case_is_kept_not_averaged_away(self):
        core.calibrate("groq", estimated=1000, actual=1400)
        core.calibrate("groq", estimated=1000, actual=1050)
        self.assertAlmostEqual(core.correction_for("groq"), 1.4, places=2)

    def test_it_is_clamped(self):
        core.calibrate("groq", estimated=100, actual=100000)
        self.assertLessEqual(core.correction_for("groq"), 2.0)

    def test_it_is_per_provider(self):
        core.calibrate("groq", estimated=1000, actual=1400)
        self.assertEqual(core.correction_for("gemini"), 1.0)


class ProviderTpmSettingTest(unittest.TestCase):
    """The 8000 is a plan, not a law."""

    def tearDown(self):
        core.set_provider_tpm("groq", 0)
        core.set_provider_tpm("gemini", 0)

    def test_a_paid_plan_is_a_number(self):
        core.set_provider_tpm("groq", 300000)
        self.assertEqual(core.PROVIDER_TPM["groq"], 300000)
        self.assertEqual(core.ceiling_for("gpt-oss-120b", "groq"), 131072)

    def test_zero_restores_what_we_know(self):
        core.set_provider_tpm("groq", 300000)
        core.set_provider_tpm("groq", 0)
        self.assertEqual(core.PROVIDER_TPM["groq"], 8000)

    def test_a_provider_we_know_nothing_about_stays_unpaced(self):
        core.set_provider_tpm("gemini", 0)
        self.assertNotIn("gemini", core.PROVIDER_TPM)

    def test_the_free_tier_still_wins_over_the_window(self):
        self.assertEqual(core.ceiling_for("gpt-oss-120b", "groq"), 8000)


class RequestTooLargeTest(unittest.TestCase):
    """A 413 is a measurement, not a rate limit."""

    class _Err(Exception):
        def __init__(self, msg, status_code=None):
            super().__init__(msg)
            self.status_code = status_code

    GROQ_413 = ("Error code: 413 - Request too large for model `qwen/qwen3.6-27b` "
                "in organization `org_x` service tier `on_demand` on tokens per "
                "minute (TPM): Limit 8000, Requested 8081, please reduce your "
                "message size and try again.")

    def test_it_reads_the_two_figures(self):
        self.assertEqual(core.request_too_large(self._Err(self.GROQ_413, 413)),
                         (8000, 8081))

    def test_an_ordinary_rate_limit_is_not_one(self):
        err = self._Err("rate_limit_exceeded: Limit 8000, Used 3704, Requested 4502. "
                        "Please try again in 1.52s.", 429)
        self.assertIsNone(core.request_too_large(err))

    def test_waiting_is_not_offered_for_a_request_that_cannot_shrink_by_waiting(self):
        # The body says `rate_limit_exceeded`, so the old code waited and re-sent
        # the identical payload, which was refused identically.
        self.assertIsNone(core.retry_after_seconds(self._Err(self.GROQ_413, 413)))


class HardLimitTest(unittest.TestCase):
    """Nothing bigger than the whole minute should ever leave the process."""

    def _conversation(self):
        return [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "the question"},
            {"role": "tool", "tool_call_id": "1", "content": "old page " * 2000},
            {"role": "tool", "tool_call_id": "2", "content": "new page " * 2000},
        ]

    def test_an_impossible_request_is_cut_down_before_it_is_sent(self):
        msgs = self._conversation()
        self.assertGreater(core.estimate_request_tokens(msgs), 8000)
        self.assertTrue(core._fit_to_hard_limit(msgs, None, "groq", 2048, 8000))
        self.assertLessEqual(core.estimate_request_tokens(msgs), 8000 - 2048)

    def test_the_newest_result_is_shortened_when_dropping_is_not_enough(self):
        msgs = self._conversation()
        core._fit_to_hard_limit(msgs, None, "groq", 2048, 8000)
        self.assertIn("truncated", msgs[3]["content"])

    def test_it_reports_defeat_when_the_cap_alone_fills_the_limit(self):
        msgs = self._conversation()
        self.assertFalse(core._fit_to_hard_limit(msgs, None, "groq", 8000, 8000))


class FlattenTest(unittest.TestCase):
    """The final round, with nothing left in it that looks like a tool session."""

    def _conversation(self):
        return [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "what is new at school?"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_url", "arguments": '{"url": "https://x"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "the page"},
        ]

    def test_no_tool_calls_survive(self):
        flat = core.flatten_tool_transcript(self._conversation())
        self.assertFalse(any(m.get("tool_calls") for m in flat))
        self.assertFalse(any(m.get("role") == "tool" for m in flat))

    def test_what_was_asked_survives(self):
        flat = core.flatten_tool_transcript(self._conversation())
        self.assertEqual(flat[0], {"role": "system", "content": "rules"})
        self.assertIn("what is new at school?", flat[1]["content"])

    def test_the_material_survives(self):
        flat = core.flatten_tool_transcript(self._conversation())
        self.assertTrue(any("the page" in (m.get("content") or "") for m in flat))

    def test_it_ends_by_asking_for_an_answer(self):
        flat = core.flatten_tool_transcript(self._conversation())
        self.assertEqual(flat[-1]["role"], "user")
        self.assertIn("Do not call any tool", flat[-1]["content"])

    def test_the_argument_json_does_not_survive(self):
        # What the model called and with what is not worth re-sending; the result
        # is. On a long argument payload this is the whole saving.
        conv = self._conversation()
        conv[2]["tool_calls"][0]["function"]["arguments"] = '{"url": "%s"}' % ("https://x/" + "y" * 900)
        flat = core.flatten_tool_transcript(conv)
        self.assertFalse(any("yyyy" in (m.get("content") or "") for m in flat))
        self.assertLess(core.estimate_messages_tokens(flat),
                        core.estimate_messages_tokens(conv))


if __name__ == "__main__":
    unittest.main()
