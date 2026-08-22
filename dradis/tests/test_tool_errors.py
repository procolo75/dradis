"""
tests/test_tool_errors.py
─────────────────────────
What happens when a tool cannot do its job.

    cd dradis && python3 -m unittest discover tests

The bug these tests pin down: a failing tool was silent. `read_url` returned the
body of Jina's error page as though it were the article, Gmail returned the
string "not authenticated" as though it were an inbox, and the model dutifully
wrote a confident summary of nothing. The user could not tell that run apart from
a good one — and the model, handed two lines of error where an article should
have been, would often call the tool again, and that extra round is what pushed
the turn over Groq's per-minute ceiling.

So a failure is now raised, not returned: the model still hears about it (a
failed tool is not a failed turn) and it is also recorded on the result, where
the Telegram layer can put it in front of the user.
"""

import contextlib
import unittest
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "dradis"))

import core                                                       # noqa: E402

from tests.addon_import import import_bot_state                   # noqa: E402


class ToolErrorLoopTest(unittest.IsolatedAsyncioTestCase):
    """The loop, driven by a stub client — no provider, no network."""

    def setUp(self):
        core.reset_tpm()

    def tearDown(self):
        core.reset_tpm()

    @staticmethod
    def _message(content=None, tool_calls=None):
        call = type("Call", (), {})
        msg  = type("Msg", (), {})()
        msg.content = content
        msg.tool_calls = None
        if tool_calls:
            msg.tool_calls = []
            for i, (name, args) in enumerate(tool_calls):
                fn = type("Fn", (), {})()
                fn.name, fn.arguments = name, args
                tc = call()
                tc.id, tc.function = f"call{i}", fn
                msg.tool_calls.append(tc)
        return msg

    def _client(self, scripted):
        """A client that returns the scripted messages, one per round."""
        outer = self

        class Completions:
            def __init__(self):
                self.calls = 0

            async def create(self, **kwargs):
                msg = scripted[min(self.calls, len(scripted) - 1)]
                self.calls += 1
                resp = type("Resp", (), {})()
                resp.choices = [type("C", (), {"message": msg})()]
                resp.usage = type("U", (), {"prompt_tokens": 100,
                                            "completion_tokens": 10})()
                return resp

        class Client:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": Completions()})()

        return Client()

    @contextlib.contextmanager
    def _stub_provider(self, client):
        """Swap out the provider SDK and the base-url lookup.

        The lookup is stubbed because it reaches into `web.store`, which drags in
        the scheduler and the Google SDKs — none of which this loop needs.
        """
        import types as _t
        stub = _t.ModuleType("openai")
        stub.AsyncOpenAI = lambda **kw: client
        sys.modules["openai"] = stub
        real_base_url = core._base_url_for_provider
        core._base_url_for_provider = lambda p: "https://stub.invalid/v1"
        try:
            yield
        finally:
            sys.modules.pop("openai", None)
            core._base_url_for_provider = real_base_url

    async def _run_agent(self, scripted, tools, model="gpt-4o", **kw):
        """Run the loop with the provider client swapped for a stub."""
        with self._stub_provider(self._client(scripted)):
            return await core.run_agent(
                "sys", "do it", tools, model, "stub", tool_call_limit=2, **kw)

    async def test_tool_error_is_recorded_and_the_turn_still_answers(self):
        async def boom():
            raise core.ToolError("HTTP 429 from r.jina.ai reading https://example.com")

        tools = [{"name": "read_url", "description": "d", "fn": boom,
                  "parameters": {"type": "object", "properties": {}}}]
        result = await self._run_agent(
            [self._message(tool_calls=[("read_url", "{}")]),
             self._message(content="I could not read the page.")],
            tools)

        self.assertEqual(result.content, "I could not read the page.")
        self.assertEqual(len(result.tool_errors), 1)
        name, reason = result.tool_errors[0]
        self.assertEqual(name, "read_url")
        self.assertIn("429", reason)

    async def test_the_model_is_told_too(self):
        # Recording the failure must not hide it from the model: it is the model
        # that decides to say "I could not read it" instead of inventing.
        seen = {}

        async def boom():
            raise core.ToolError("HTTP 451")

        tools = [{"name": "read_url", "description": "d", "fn": boom,
                  "parameters": {"type": "object", "properties": {}}}]
        client = self._client([self._message(tool_calls=[("read_url", "{}")]),
                               self._message(content="done")])
        real_create = client.chat.completions.create

        async def spy(**kwargs):
            seen["messages"] = list(kwargs["messages"])
            return await real_create(**kwargs)

        client.chat.completions.create = spy
        with self._stub_provider(client):
            await core.run_agent("sys", "do it", tools, "gpt-4o", "stub",
                                 tool_call_limit=2)

        tool_msgs = [m for m in seen["messages"] if m.get("role") == "tool"]
        self.assertTrue(any("451" in m["content"] for m in tool_msgs))

    async def test_a_working_tool_records_nothing(self):
        async def fine():
            return "the page text"

        tools = [{"name": "read_url", "description": "d", "fn": fine,
                  "parameters": {"type": "object", "properties": {}}}]
        result = await self._run_agent(
            [self._message(tool_calls=[("read_url", "{}")]),
             self._message(content="here it is")],
            tools)
        self.assertEqual(result.tool_errors, [])
        self.assertEqual(result.tools_used, ["read_url"])

    async def test_unexpected_exceptions_are_recorded_as_well(self):
        async def crash():
            raise KeyError("nope")

        tools = [{"name": "read_url", "description": "d", "fn": crash,
                  "parameters": {"type": "object", "properties": {}}}]
        result = await self._run_agent(
            [self._message(tool_calls=[("read_url", "{}")]),
             self._message(content="sorry")],
            tools)
        self.assertEqual(len(result.tool_errors), 1)
        self.assertIn("KeyError", result.tool_errors[0][1])

    async def test_an_unknown_tool_is_a_failure_too(self):
        result = await self._run_agent(
            [self._message(tool_calls=[("ghost", "{}")]),
             self._message(content="sorry")],
            [])
        self.assertEqual(result.tool_errors, [("ghost", "unknown tool")])

    async def test_tool_results_are_cut_to_the_budget(self):
        # A model on an 8K window must not be handed a 40K-character page.
        async def huge():
            return "x" * 40000

        tools = [{"name": "read_url", "description": "d", "fn": huge,
                  "parameters": {"type": "object", "properties": {}}}]
        client = self._client([self._message(tool_calls=[("read_url", "{}")]),
                               self._message(content="done")])
        sent = {}
        real_create = client.chat.completions.create

        async def spy(**kwargs):
            sent["messages"] = [dict(m) for m in kwargs["messages"]]
            return await real_create(**kwargs)

        client.chat.completions.create = spy
        with self._stub_provider(client):
            await core.run_agent("sys", "do it", tools, "llama-3.3-70b", "stub",
                                 max_tokens=2048, tool_call_limit=2)

        tool_msgs = [m for m in sent["messages"] if m.get("role") == "tool"]
        self.assertTrue(tool_msgs)
        self.assertLess(len(tool_msgs[0]["content"]), 40000)
        self.assertLessEqual(
            core.estimate_messages_tokens(sent["messages"]),
            core.context_window_for("llama-3.3-70b") - 2048)


class ReadUrlTest(unittest.IsolatedAsyncioTestCase):
    """`read_url` against a stubbed httpx — the status check is the point."""

    def setUp(self):
        self.st = import_bot_state()

    @contextlib.contextmanager
    def _http(self, status=200, text="the article"):
        import types as _t
        captured = {}

        class Resp:
            status_code = status

        Resp.text = text

        class Client:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def get(self_inner, url, **kw):
                captured["url"] = url
                return Resp()

        stub = _t.ModuleType("httpx")
        stub.AsyncClient = lambda **kw: Client()
        real = sys.modules.get("httpx")
        sys.modules["httpx"] = stub
        try:
            yield captured
        finally:
            if real is not None:
                sys.modules["httpx"] = real
            else:
                sys.modules.pop("httpx", None)

    async def test_a_good_page_comes_back(self):
        with self._http(200, "the article"):
            self.assertEqual(await self.st.read_url("https://example.com"), "the article")

    async def test_jina_rate_limit_raises_instead_of_masquerading_as_the_page(self):
        # This is the whole bug: 8 lines of "rate limit exceeded" used to reach the
        # model as if they were the article.
        with self._http(429, "rate limit exceeded, try later"):
            with self.assertRaises(self.st.agent_core.ToolError) as ctx:
                await self.st.read_url("https://example.com")
        self.assertIn("429", str(ctx.exception))
        self.assertNotIn("rate limit exceeded, try later", str(ctx.exception))

    async def test_a_404_raises_too(self):
        with self._http(404, "<html>Not Found</html>"):
            with self.assertRaises(self.st.agent_core.ToolError):
                await self.st.read_url("https://example.com/gone")

    async def test_a_bad_url_raises_rather_than_returning_a_sentence(self):
        with self.assertRaises(self.st.agent_core.ToolError):
            await self.st.read_url("example.com")

    async def test_the_url_is_encoded(self):
        with self._http() as captured:
            await self.st.read_url("https://example.com/a b")
        self.assertNotIn(" ", captured["url"])

    async def test_the_safety_cap_still_applies(self):
        with self._http(200, "x" * 50000):
            page = await self.st.read_url("https://example.com")
        self.assertEqual(len(page), self.st._READ_URL_MAX_CHARS)


class ToolErrorsMessageTest(unittest.TestCase):
    """The Telegram notice."""

    def setUp(self):
        self.st = import_bot_state()

    @staticmethod
    def _result(errors):
        return core.AgentResult(content="an answer", tool_errors=errors)

    def test_nothing_to_report_produces_nothing(self):
        self.assertEqual(self.st._tool_errors_msg({}, self._result([])), "")
        self.assertEqual(self.st._tool_errors_msg({}, None), "")

    def test_a_failure_names_the_tool_and_the_reason(self):
        msg = self.st._tool_errors_msg({}, self._result([("read_url", "HTTP 429")]))
        self.assertIn("read_url", msg)
        self.assertIn("HTTP 429", msg)

    def test_the_same_tool_failing_twice_is_reported_once(self):
        # A model that retried a failing fetch hit one problem, not two.
        msg = self.st._tool_errors_msg(
            {}, self._result([("read_url", "HTTP 429"), ("read_url", "HTTP 429")]))
        self.assertEqual(msg.count("read_url"), 1)

    def test_distinct_tools_each_get_a_line(self):
        msg = self.st._tool_errors_msg(
            {}, self._result([("read_url", "HTTP 429"), ("get_emails", "not authenticated")]))
        self.assertIn("read_url", msg)
        self.assertIn("get_emails", msg)

    def test_the_setting_switches_it_off(self):
        result = self._result([("read_url", "HTTP 429")])
        self.assertEqual(self.st._tool_errors_msg({"tool_errors_enabled": False}, result), "")
        self.assertNotEqual(self.st._tool_errors_msg({"tool_errors_enabled": True}, result), "")

    def test_it_is_on_unless_switched_off(self):
        # Default True: a silent failure is the thing being fixed, so silence has
        # to be the choice you make, not the one you get.
        self.assertNotEqual(self.st._tool_errors_msg({}, self._result([("t", "r")])), "")

    def test_the_reason_is_escaped_for_telegram_html(self):
        msg = self.st._tool_errors_msg({}, self._result([("read_url", "got <html> back")]))
        self.assertNotIn("<html>", msg)
        self.assertIn("&lt;html&gt;", msg)

    def test_a_task_name_is_carried(self):
        msg = self.st._tool_errors_msg({}, self._result([("read_url", "x")]), "Rassegna stampa")
        self.assertIn("Rassegna stampa", msg)


class FailureSurvivesTrimmingTest(unittest.IsolatedAsyncioTestCase):
    """A failure message must never be the thing the budget throws away."""

    @staticmethod
    def _message(content=None, tool_calls=None):
        return ToolErrorLoopTest._message(content, tool_calls)

    async def test_the_reason_reaches_the_model_even_with_no_room_left(self):
        async def boom():
            raise core.ToolError("HTTP 429 from r.jina.ai reading https://example.com")

        tools = [{"name": "read_url", "description": "d", "fn": boom,
                  "parameters": {"type": "object", "properties": {}}}]
        test = ToolErrorLoopTest()
        client = test._client([self._message(tool_calls=[("read_url", "{}")]),
                               self._message(content="could not read it")])
        sent = {}
        real_create = client.chat.completions.create

        async def spy(**kwargs):
            sent["messages"] = [dict(m) for m in kwargs["messages"]]
            return await real_create(**kwargs)

        client.chat.completions.create = spy
        with test._stub_provider(client):
            # A system prompt that already eats the whole window: without a floor
            # the tool result would be trimmed to the marker and the 429 lost.
            result = await core.run_agent(
                "x" * 30000, "do it", tools, "llama-3.3-70b", "stub",
                max_tokens=2048, tool_call_limit=2)

        tool_msgs = [m for m in sent["messages"] if m.get("role") == "tool"]
        self.assertTrue(any("429" in m["content"] for m in tool_msgs))
        self.assertEqual(len(result.tool_errors), 1)


if __name__ == "__main__":
    unittest.main()
