# Fixtures

`jina_school_news_archive.md` — what `r.jina.ai` returned for
`https://www.isistassinari.edu.it/archivio-news` on 2026-08-22, in its default
readability mode: 11 615 characters of month menu, eighteen signed thumbnail
URLs and a cookie banner, containing **none** of the eighteen headlines that are
in the page's HTML. This is the input that made a scheduled task call `read_url`
three times and end on a refused request.

`jina_school_news_archive.text.md` — the same URL with `X-Respond-With: text`:
6 584 characters, all eighteen headlines present. Captured in the same minute.

Both are kept verbatim so the numbers in `core.MIN_PROSE_SHARE`'s comment can be
re-derived rather than trusted.
