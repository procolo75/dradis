# Wiki mirror

This directory mirrors the **GitHub Wiki** at <https://github.com/procolo75/dradis/wiki>
(a separate git repository: `https://github.com/procolo75/dradis.wiki.git`).

It exists so wiki edits are reviewable in normal PRs and so drift between the two
is visible in `git diff`. It is *not* automatically published — the wiki repo has
to be pushed separately.

## Keeping the two in sync

The mirror is the source of truth. On every release:

```sh
git clone https://github.com/procolo75/dradis.wiki.git /tmp/dradis-wiki
cp dradis/wiki/*.md /tmp/dradis-wiki/          # README.md is mirror-only, see below
rm /tmp/dradis-wiki/README.md
cd /tmp/dradis-wiki && git add -A && git commit -m "docs(wiki): vX.Y.Z" && git push
```

To check for drift before editing:

```sh
diff -q dradis/wiki/*.md /tmp/dradis-wiki/
```

## Notes

- **`CHANGELOG.md` is a copy of `../CHANGELOG.md`.** The wiki needs it as a page,
  so it is duplicated here rather than symlinked (the wiki repo does not follow
  symlinks). Copy it across whenever the real changelog changes.
- **`_Sidebar.md` and `Home.md` are navigation.** A new page must be added to both,
  or it is published but unreachable.
- **`README.md` (this file) is mirror-only** and must not be copied to the wiki —
  GitHub would render it as a wiki page named "README".

Drift found on 2026-08-09 that this procedure is meant to prevent: `Monitors.md`
had diverged in both directions at once (the mirror carried the current TRS
formula, the wiki carried the Weather Charts section), `Examples.md` still
described the lightning algorithm from v2.17.0 — two rewrites out of date — and
the wiki `CHANGELOG.md` had stopped at 3.2.1.
