# Fixtures

`jina_school_news_archive.md` — what `r.jina.ai` returned for
`https://www.isistassinari.edu.it/archivio-news` on 2026-08-22, in its default
readability mode: 11 615 characters of month menu, eighteen signed thumbnail
URLs and a cookie banner, containing **none** of the eighteen headlines that are
in the page's HTML. This is the input that made a scheduled task call `read_url`
three times and end on a refused request.

`jina_school_news_archive.text.md` — the same URL with `X-Respond-With: text`:
6 584 characters, all eighteen headlines present. Captured in the same minute.

`jina_pretemp_home.links.json` — what `r.jina.ai` returned for
`https://www.pretemp.it/` on 2026-09-09 with `Accept: application/json` and
`X-With-Links-Summary: all`: 71 links, and `data.content` holding the same 3 554
characters of well-formed prose the default mode returns. That page scores 0.71
on the prose share, so nothing about it looks broken — and **not one** of the
three `/previsioni/NNNN` links that are in its HTML is in the markdown. It is the
opposite face of the school archive: there the content was missing and the
addresses were there, here the content is there and the addresses are missing.
The same link appears three times under three labels, which is why
`core.format_links` keeps the longest.

All three are kept verbatim so the numbers in `core.MIN_PROSE_SHARE`'s comment
and in `format_links`'s can be re-derived rather than trusted.
