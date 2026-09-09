"""
tests/test_read_url_links.py
────────────────────────────
`read_url`'s link branch: finding the page you actually have to read.

    cd dradis && python3 -m unittest discover tests

v4.5.1 taught `read_url` to notice when Jina's readability pass had kept the
wrong part of a page — the school news archive that came back as 11 615
characters of month menu with none of its eighteen headlines. The measure was
the share of prose, because that failure looks like a wall of addresses.

pretemp.it is the same failure wearing the opposite face. Its home page comes
back as 3 626 characters of well-formed Italian — prose share 0.71, so nothing
about it looks wrong — and the three links to the forecasts that are in its
HTML are not in it. Nor is there a stable address to skip the page with:
/previsioni answers 204 and /previsioni/oggi, /latest, /ultima, /feed and
/sitemap.xml all answer 404, and the forecast's id changes every day.

So the page is asked for its links instead of its text, and what comes back is
a menu: four lines instead of four thousand characters, which is what keeps a
two-hop read inside Groq's 8 000 tokens a minute.

The fixture is the real answer, captured on 2026-09-09.
"""

import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "dradis"))

import core                                                       # noqa: E402

from tests.addon_import import import_bot_state                   # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "jina_pretemp_home.links.json"
_HOME    = "https://www.pretemp.it/"


def _pairs():
    """The link summary r.jina.ai returned for pretemp.it's home page."""
    return [(str(a), str(b))
            for a, b in json.loads(_FIXTURE.read_text(encoding="utf-8"))["data"]["links"]]


class FormatLinksTest(unittest.TestCase):
    """`core.format_links` — the menu, from the reader's answer."""

    def test_the_forecast_link_survives_the_filter(self):
        out = core.format_links(_pairs(), "previsione", _HOME)
        self.assertIn("https://www.pretemp.it/previsioni/3521", out)
        self.assertIn("Ultima previsione", out)

    def test_the_label_kept_for_an_address_is_the_one_that_identifies_it(self):
        """3521 is listed three times; only one of the three says which it is.

        Dropping the duplicates is the point — but dropping the wrong two of
        the three would leave the model choosing between "Previsione" and
        "Previsione", which is not a choice.
        """
        out = core.format_links(_pairs(), "previsione", _HOME)
        self.assertEqual(1, out.count("/previsioni/3521"))
        self.assertIn("Ultima previsione Pericolosità 2 09 settembre 2026", out)

    def test_the_menu_is_smaller_than_the_page_it_replaces(self):
        """3 554 characters of page against a few hundred of menu."""
        page = len(json.loads(_FIXTURE.read_text(encoding="utf-8"))["data"]["content"])
        self.assertLess(len(core.format_links(_pairs(), "previsione", _HOME)), page // 4)

    def test_a_dict_summary_reads_the_same_as_a_list_of_pairs(self):
        """The reader answers with pairs; its documentation promises a mapping."""
        pairs = [("Ultima previsione", "https://x/1"), ("Archivio", "https://x/2")]
        self.assertEqual(core.format_links(pairs, "", _HOME),
                         core.format_links(list(dict(pairs).items()), "", _HOME))

    def test_the_filter_matches_the_address_too(self):
        pairs = [("Vai all'archivio", "https://www.pretemp.it/previsioni/3521"),
                 ("Radar", "https://www.pretemp.it/monitoraggio/radar")]
        out = core.format_links(pairs, "previsioni", _HOME)
        self.assertIn("/previsioni/3521", out)
        self.assertNotIn("radar", out)

    def test_the_filter_ignores_case(self):
        pairs = [("ULTIMA PREVISIONE", "https://x/1")]
        self.assertIn("https://x/1", core.format_links(pairs, "previsione", _HOME))

    def test_an_empty_filter_returns_every_link(self):
        out = core.format_links(_pairs(), "", _HOME)
        self.assertIn("https://www.pretemp.it/modelli", out)
        self.assertIn("https://www.pretemp.it/previsioni/3521", out)

    def test_a_long_label_is_cut_and_says_so(self):
        pairs = [("x" * 400, "https://x/1")]
        line = core.format_links(pairs, "", _HOME)
        self.assertLess(len(line), 400)
        self.assertIn("…", line)

    def test_a_label_written_over_three_lines_arrives_as_one(self):
        pairs = [("Ultima\n  previsione\n\tdi oggi", "https://x/1")]
        out = core.format_links(pairs, "", _HOME)
        self.assertEqual(1, len(out.splitlines()))
        self.assertIn("Ultima previsione di oggi", out)

    def test_a_link_with_no_label_still_gives_its_address(self):
        self.assertEqual("- https://x/1", core.format_links([("", "https://x/1")], "", _HOME))

    def test_more_links_than_the_cap_are_cut_and_the_cut_is_reported(self):
        pairs = [(f"link {i}", f"https://x/{i}") for i in range(core.MAX_LINKS + 12)]
        out = core.format_links(pairs, "", _HOME)
        self.assertEqual(core.MAX_LINKS + 1, len(out.splitlines()))
        self.assertIn("12 more links not shown", out)

    def test_no_match_says_so_instead_of_saying_nothing(self):
        """An empty answer is what taught the model to invent arguments."""
        out = core.format_links(_pairs(), "terremoto", _HOME)
        self.assertTrue(out.strip())
        self.assertIn("terremoto", out)
        self.assertIn(_HOME, out)


class ReadUrlLinkBranchTest(unittest.IsolatedAsyncioTestCase):
    """`read_url(url, links=…)` — the branch, and the one it must not disturb."""

    @classmethod
    def setUpClass(cls):
        cls.state = import_bot_state()

    def _client(self, *, status=200, body=None, text=""):
        state = self.state

        class Response:
            status_code = status
            def json(self):
                if body is None:
                    raise ValueError("Expecting value: line 1 column 1 (char 0)")
                return body
            @property
            def text(self):
                return text

        class Client:
            def __init__(self):
                self.headers_seen = []
            async def __aenter__(self):  return self
            async def __aexit__(self, *a):  return False
            async def get(self, url, headers=None, follow_redirects=False):
                self.headers_seen.append(headers or {})
                return Response()

        client = Client()
        import httpx
        self.addCleanup(setattr, httpx, "AsyncClient", httpx.AsyncClient)
        httpx.AsyncClient = lambda *a, **k: client
        return state, client

    async def test_the_branch_asks_the_reader_for_links_and_returns_a_menu(self):
        payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        state, client = self._client(body=payload)
        out = await state.read_url(_HOME, links="previsione")
        self.assertIn("/previsioni/3521", out)
        self.assertEqual("all", client.headers_seen[0]["X-With-Links-Summary"])
        self.assertEqual("application/json", client.headers_seen[0]["Accept"])

    async def test_one_fetch_only(self):
        """The prose check belongs to the other branch; this one must not pay it."""
        payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        state, client = self._client(body=payload)
        await state.read_url(_HOME, links="")
        self.assertEqual(1, len(client.headers_seen))

    async def test_a_rate_limited_reader_fails_instead_of_answering(self):
        state, _ = self._client(status=429, body={})
        with self.assertRaises(core.ToolError) as caught:
            await state.read_url(_HOME, links="previsione")
        self.assertIn("429", str(caught.exception))
        self.assertIn(_HOME, str(caught.exception))

    async def test_a_body_that_is_not_json_is_a_tool_failure_not_a_crash(self):
        state, _ = self._client(body=None)
        with self.assertRaises(core.ToolError):
            await state.read_url(_HOME, links="previsione")

    async def test_a_json_body_without_a_link_summary_is_a_tool_failure(self):
        state, _ = self._client(body={"data": {"content": "..."}})
        with self.assertRaises(core.ToolError):
            await state.read_url(_HOME, links="previsione")

    async def test_a_link_summary_of_the_wrong_shape_is_a_tool_failure(self):
        state, _ = self._client(body={"data": {"links": "nope"}})
        with self.assertRaises(core.ToolError):
            await state.read_url(_HOME, links="previsione")

    async def test_a_bad_url_is_refused_before_any_fetch(self):
        state, client = self._client(body={"data": {"links": []}})
        with self.assertRaises(core.ToolError):
            await state.read_url("pretemp.it", links="previsione")
        self.assertEqual([], client.headers_seen)

    async def test_without_links_the_page_is_read_exactly_as_before(self):
        state, client = self._client(text="# Titolo\n\nUna saccatura atlantica approccia.")
        out = await state.read_url(_HOME)
        self.assertIn("saccatura", out)
        self.assertEqual("text/plain", client.headers_seen[0]["Accept"])
        self.assertNotIn("X-With-Links-Summary", client.headers_seen[0])


class ToolSchemaTest(unittest.TestCase):
    """What the model is told the tool can do."""

    @classmethod
    def setUpClass(cls):
        cls.state = import_bot_state()

    def test_links_is_offered_and_stays_optional(self):
        params = self.state.READ_URL_TOOL["parameters"]
        self.assertIn("links", params["properties"])
        self.assertEqual(["url"], params["required"])

    def test_the_description_says_when_to_reach_for_it(self):
        """A parameter nobody knows the use of is a parameter nobody uses."""
        self.assertIn("links", self.state.READ_URL_TOOL["description"])


if __name__ == "__main__":
    unittest.main()
