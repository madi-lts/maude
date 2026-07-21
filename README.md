# maude
Math-focused adaptation of Claude Code

## claude-transcript-viewer

Local web app for browsing Claude Code transcripts
(`~/.claude/projects/*.jsonl`) with proper math rendering. Two files:
`claude-transcript-viewer.py` (stdlib-only server + JSONL parsing) and
`viewer.html` (the page, served from next to the script and re-read on
every request, so front-end edits just need a refresh).

```
python3 claude-transcript-viewer.py            # serve on localhost:8483 + open browser
python3 claude-transcript-viewer.py --help     # --port, --root, --host, --no-browser
```

No dependencies beyond the Python stdlib; the page pulls marked, DOMPurify and
KaTeX from jsDelivr, so the *browser* needs network access (the server binds to
localhost only).

- **Sidebar** — projects (labeled from the `cwd` recorded in transcripts) with
  sessions sorted by last modified, titled by summary or first user message.
- **Math** — `$…$`, `$$…$$`, `\(…\)`, `\[…\]`, and bare `\begin{align}`-style
  environments all render with KaTeX. Math inside code fences and inline code
  stays literal, and lone currency `$`s are left alone.
- **Copy TeX** — double-click any rendered equation to copy its exact TeX
  source (read from KaTeX's embedded `<annotation>` element);
  shift-double-click copies it wrapped in delimiters (`$…$` / `$$…$$`) for
  pasting into other KaTeX-backed apps.
- **Tool calls / thinking** — rendered as collapsed blocks so they don't drown
  out the conversation.
- **Search** — the sidebar box greps text and thinking blocks across every
  project; click a hit to open its session.
