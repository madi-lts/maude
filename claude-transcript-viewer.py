#!/usr/bin/env python3
"""claude-transcript-viewer — browse Claude Code transcripts with real math rendering.

Single-file local web app, stdlib only. Serves a small JSON API over
~/.claude/projects/ and one embedded HTML page that renders conversations
with marked (markdown) + KaTeX (math). Double-click any rendered equation
to copy its TeX source; shift-double-click copies it with delimiters.

Usage:
    python3 claude-transcript-viewer.py                  # serve + open browser
    python3 claude-transcript-viewer.py --port 9000
    python3 claude-transcript-viewer.py --root /path/to/projects --no-browser

The page loads marked, DOMPurify and KaTeX from jsDelivr, so the browser
needs network access; the server itself binds to localhost only.
"""

import argparse
import json
import os
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")

# cheap per-line check for countable records, tolerant of JSON spacing
MSG_TYPE_RE = re.compile(r'"type"\s*:\s*"(?:user|assistant)"')

# --------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------


def safe_join(root, *parts):
    """Join and refuse anything that escapes root (path traversal guard)."""
    path = os.path.realpath(os.path.join(root, *parts))
    if not path.startswith(os.path.realpath(root) + os.sep):
        raise ValueError("path escapes root")
    return path


def iter_jsonl(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def session_files(project_dir):
    try:
        names = os.listdir(project_dir)
    except OSError:
        return []
    return [n for n in names if n.endswith(".jsonl")]


def project_label(project_dir, dirname):
    """Prefer the cwd recorded inside a transcript; fall back to un-mangling
    the directory name (ambiguous when the real path contains hyphens)."""
    files = sorted(
        session_files(project_dir),
        key=lambda n: os.path.getmtime(os.path.join(project_dir, n)),
        reverse=True,
    )
    for name in files[:3]:
        try:
            for i, obj in enumerate(iter_jsonl(os.path.join(project_dir, name))):
                if i > 50:
                    break
                cwd = obj.get("cwd")
                if cwd:
                    return cwd
        except OSError:
            continue
    if dirname.startswith("-"):
        return dirname.replace("-", "/")
    return dirname


def list_projects(root):
    out = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        files = session_files(entry.path)
        if not files:
            continue
        mtime = max(os.path.getmtime(os.path.join(entry.path, f)) for f in files)
        out.append(
            {
                "dir": entry.name,
                "label": project_label(entry.path, entry.name),
                "sessions": len(files),
                "mtime": mtime,
            }
        )
    out.sort(key=lambda p: -p["mtime"])
    return out


def first_text(content):
    """Best-effort human-readable text from a message content value."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def session_title(path):
    """Summary record if present, else the first real user message."""
    fallback = None
    for obj in iter_jsonl(path):
        kind = obj.get("type")
        if kind == "summary" and obj.get("summary"):
            return obj["summary"]
        if fallback is None and kind == "user" and not obj.get("isMeta"):
            msg = obj.get("message") or {}
            text = first_text(msg.get("content")).strip()
            # skip harness-injected turns like <command-name>… wrappers
            if text and not text.startswith("<"):
                fallback = text
    return fallback or "(no user message)"


def list_sessions(root, project):
    pdir = safe_join(root, project)
    out = []
    for name in session_files(pdir):
        path = os.path.join(pdir, name)
        title = session_title(path)
        if len(title) > 120:
            title = title[:120] + "…"
        count = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if MSG_TYPE_RE.search(line):
                    count += 1
        out.append(
            {
                "id": name[:-6],
                "title": title,
                "messages": count,
                "mtime": os.path.getmtime(path),
            }
        )
    out.sort(key=lambda s: -s["mtime"])
    return out


def tool_result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return ""


MAX_BLOCK = 40_000  # chars; keeps giant tool dumps from choking the page


def load_messages(root, project, session):
    path = safe_join(root, project, session + ".jsonl")
    messages = []
    for obj in iter_jsonl(path):
        if obj.get("type") not in ("user", "assistant"):
            continue
        if obj.get("isSidechain"):
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        blocks = []
        if isinstance(content, str):
            blocks.append({"type": "text", "text": content[:MAX_BLOCK]})
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    blocks.append({"type": "text", "text": b.get("text", "")[:MAX_BLOCK]})
                elif t == "thinking":
                    blocks.append(
                        {"type": "thinking", "text": b.get("thinking", "")[:MAX_BLOCK]}
                    )
                elif t == "tool_use":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "name": b.get("name", "?"),
                            "input": json.dumps(
                                b.get("input", {}), indent=2, ensure_ascii=False
                            )[:MAX_BLOCK],
                        }
                    )
                elif t == "tool_result":
                    blocks.append(
                        {"type": "tool_result", "text": tool_result_text(b)[:MAX_BLOCK]}
                    )
                elif t == "image":
                    blocks.append({"type": "note", "text": "[image]"})
        if not blocks:
            continue
        # Assistant turns arrive split across records sharing one message id.
        mid = msg.get("id")
        if messages and mid and messages[-1]["mid"] == mid:
            messages[-1]["blocks"].extend(blocks)
        else:
            messages.append(
                {
                    "role": msg.get("role") or obj["type"],
                    "ts": obj.get("timestamp"),
                    "mid": mid,
                    "meta": bool(obj.get("isMeta")),
                    "blocks": blocks,
                }
            )
    for m in messages:
        del m["mid"]
    return messages


def search_transcripts(root, query, limit=100):
    """Case-insensitive substring search over text/thinking blocks.

    Lines are pre-filtered with a raw substring check before JSON parsing,
    so matches hidden behind \\uXXXX escapes can be missed — acceptable for
    an interactive grep."""
    q = query.lower()
    results = []
    for proj in list_projects(root):
        pdir = os.path.join(root, proj["dir"])
        for name in session_files(pdir):
            path = os.path.join(pdir, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if q not in line.lower():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") not in ("user", "assistant"):
                        continue
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    texts = []
                    if isinstance(content, str):
                        texts.append(content)
                    elif isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") in ("text", "thinking"):
                                texts.append(b.get("text") or b.get("thinking") or "")
                    for text in texts:
                        idx = text.lower().find(q)
                        if idx < 0:
                            continue
                        start = max(0, idx - 80)
                        end = min(len(text), idx + len(query) + 80)
                        results.append(
                            {
                                "project": proj["dir"],
                                "label": proj["label"],
                                "session": name[:-6],
                                "role": msg.get("role") or obj.get("type"),
                                "snippet": text[start:end],
                            }
                        )
                        if len(results) >= limit:
                            return results
                        break  # one hit per record is plenty
    return results


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    root = DEFAULT_ROOT

    def do_GET(self):
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("", "/"):
                self.respond(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/projects":
                self.send_json(list_projects(self.root))
            elif url.path == "/api/sessions":
                self.send_json(list_sessions(self.root, params["project"]))
            elif url.path == "/api/messages":
                self.send_json(
                    load_messages(self.root, params["project"], params["session"])
                )
            elif url.path == "/api/search":
                query = params.get("q", "").strip()
                self.send_json(search_transcripts(self.root, query) if query else [])
            else:
                self.send_error(404)
        except (KeyError, ValueError) as exc:
            self.send_json({"error": str(exc)}, status=400)
        except OSError as exc:
            self.send_json({"error": str(exc)}, status=500)

    def send_json(self, obj, status=200):
        self.respond(json.dumps(obj).encode("utf-8"), "application/json", status)

    def respond(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# --------------------------------------------------------------------------
# The page. Raw string: backslashes below belong to the embedded JS/CSS.
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude transcripts</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<style>
:root {
  --bg: #f7f6f3; --panel: #efede8; --fg: #29261f; --dim: #7a7568;
  --accent: #b0562c; --border: #ddd8cd; --user-bg: #e8e4da; --code-bg: #eceae3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1e1c19; --panel: #26231f; --fg: #e6e1d6; --dim: #948d7d;
    --accent: #e08a56; --border: #3a362f; --user-bg: #2d2a24; --code-bg: #2a2721;
  }
}
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.55 system-ui, sans-serif; background: var(--bg); color: var(--fg); }
#app { display: flex; height: 100vh; }
aside { width: 320px; min-width: 240px; flex-shrink: 0; border-right: 1px solid var(--border);
        background: var(--panel); display: flex; flex-direction: column; }
#search { margin: 10px; padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px;
          background: var(--bg); color: var(--fg); font: inherit; }
#search:focus { outline: 1px solid var(--accent); }
#sidebar-list { overflow-y: auto; flex: 1; padding-bottom: 20px; }
.project > .pname { padding: 8px 12px; cursor: pointer; font-weight: 600; font-size: 13px;
                    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.project > .pname:hover { color: var(--accent); }
.project .count { color: var(--dim); font-weight: 400; }
.session { padding: 6px 12px 6px 22px; cursor: pointer; border-left: 2px solid transparent; }
.session:hover { background: var(--user-bg); }
.session.active { border-left-color: var(--accent); background: var(--user-bg); }
.session .stitle { font-size: 13px; overflow: hidden; display: -webkit-box;
                   -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.session .sdate, .hit .sdate { font-size: 11px; color: var(--dim); }
.hit { padding: 6px 12px; cursor: pointer; border-bottom: 1px solid var(--border); font-size: 13px; }
.hit:hover { background: var(--user-bg); }
.hit mark { background: var(--accent); color: var(--bg); border-radius: 2px; padding: 0 1px; }
main { flex: 1; overflow-y: auto; padding: 24px clamp(16px, 5vw, 64px); }
#title { font-size: 14px; color: var(--dim); margin-bottom: 18px; }
.msg { margin-bottom: 22px; max-width: 55em; }
.msg .mhead { font-size: 12px; color: var(--dim); margin-bottom: 4px; }
.msg.user .mhead { color: var(--accent); }
.msg.user .md { background: var(--user-bg); border-radius: 8px; padding: 10px 14px; }
.msg.meta { opacity: 0.55; }
details.blk { margin: 6px 0; border: 1px solid var(--border); border-radius: 6px; }
details.blk > summary { cursor: pointer; padding: 4px 10px; font-size: 12px; color: var(--dim);
                        user-select: none; }
details.blk > .inner { padding: 4px 12px 8px; border-top: 1px solid var(--border); }
details.blk pre { margin: 6px 0; }
.md pre, details.blk pre { background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 10px 12px; overflow-x: auto; font-size: 13px; line-height: 1.45;
  white-space: pre-wrap; word-break: break-word; }
.md code { background: var(--code-bg); border-radius: 3px; padding: 1px 4px; font-size: 0.9em; }
.md pre code { background: none; padding: 0; }
.md blockquote { border-left: 3px solid var(--border); margin-left: 0; padding-left: 12px; color: var(--dim); }
.md table { border-collapse: collapse; }
.md th, .md td { border: 1px solid var(--border); padding: 4px 10px; }
.md img { max-width: 100%; }
.md a { color: var(--accent); }
.katex { cursor: pointer; }
.katex-display { overflow-x: auto; overflow-y: hidden; padding: 4px 0; }
.katex:hover { background: color-mix(in srgb, var(--accent) 12%, transparent); border-radius: 3px; }
#toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  background: var(--fg); color: var(--bg); padding: 8px 18px; border-radius: 20px;
  font-size: 13px; opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 10; }
#toast.show { opacity: 1; }
.empty { color: var(--dim); padding: 40px 0; text-align: center; }
</style>
</head>
<body>
<div id="app">
  <aside>
    <input id="search" type="search" placeholder="Search all transcripts…" autocomplete="off">
    <div id="sidebar-list"></div>
  </aside>
  <main>
    <div id="title"></div>
    <div id="messages"><div class="empty">Pick a session on the left.<br>
      Double-click any equation to copy its TeX source (shift for delimiters).</div></div>
  </main>
</div>
<div id="toast"></div>

<script>
"use strict";
window.addEventListener("DOMContentLoaded", init);

let projects = [];
let activeSession = null;

async function api(path) {
  const r = await fetch(path);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

async function init() {
  marked.use({ gfm: true, breaks: true });
  projects = await api("/api/projects");
  renderProjectList();
  const search = document.getElementById("search");
  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => runSearch(search.value.trim()), 300);
  });
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderProjectList() {
  const list = document.getElementById("sidebar-list");
  list.textContent = "";
  if (!projects.length) {
    list.append(el("div", "empty", "No projects found under the transcript root."));
    return;
  }
  for (const p of projects) {
    const box = el("div", "project");
    const name = el("div", "pname");
    name.append(document.createTextNode(p.label + " "));
    name.append(el("span", "count", "(" + p.sessions + ")"));
    name.title = p.label;
    const sessions = el("div", "sessions");
    sessions.hidden = true;
    let loaded = false;
    name.addEventListener("click", async () => {
      sessions.hidden = !sessions.hidden;
      if (loaded || sessions.hidden) return;
      loaded = true;
      const items = await api("/api/sessions?project=" + encodeURIComponent(p.dir));
      for (const s of items) {
        const row = el("div", "session");
        row.dataset.key = p.dir + "/" + s.id;
        row.append(el("div", "stitle", s.title));
        row.append(el("div", "sdate",
          new Date(s.mtime * 1000).toLocaleString() + " · " + s.messages + " msgs"));
        row.addEventListener("click", () => openSession(p, s.id, s.title));
        sessions.append(row);
      }
    });
    box.append(name, sessions);
    list.append(box);
  }
}

async function openSession(project, sessionId, title) {
  activeSession = project.dir + "/" + sessionId;
  for (const row of document.querySelectorAll(".session"))
    row.classList.toggle("active", row.dataset.key === activeSession);
  document.getElementById("title").textContent = project.label + " — " + (title || sessionId);
  const pane = document.getElementById("messages");
  pane.textContent = "Loading…";
  try {
    const msgs = await api("/api/messages?project=" + encodeURIComponent(project.dir) +
                           "&session=" + encodeURIComponent(sessionId));
    pane.textContent = "";
    if (!msgs.length) pane.append(el("div", "empty", "No displayable messages."));
    for (const m of msgs) pane.append(renderMessage(m));
  } catch (err) {
    pane.textContent = "";
    pane.append(el("div", "empty", "Failed to load session: " + err.message));
  }
  document.querySelector("main").scrollTop = 0;
}

function renderMessage(m) {
  const box = el("div", "msg " + m.role + (m.meta ? " meta" : ""));
  const head = el("div", "mhead",
    (m.role === "user" ? "You" : "Claude") +
    (m.ts ? " · " + new Date(m.ts).toLocaleString() : ""));
  box.append(head);
  for (const b of m.blocks) {
    if (b.type === "text") box.append(renderMarkdown(b.text));
    else if (b.type === "thinking") box.append(collapsible("thinking", renderMarkdown(b.text)));
    else if (b.type === "tool_use") box.append(collapsible("tool: " + b.name, pre(b.input)));
    else if (b.type === "tool_result") box.append(collapsible("tool result", pre(b.text)));
    else if (b.type === "note") box.append(el("div", "mhead", b.text));
  }
  return box;
}

function collapsible(label, body) {
  const d = el("details", "blk");
  d.append(el("summary", null, label));
  const inner = el("div", "inner");
  inner.append(body);
  d.append(inner);
  return d;
}

function pre(text) {
  const node = el("pre");
  node.textContent = text || "(empty)";
  return node;
}

/* ---- markdown + math ----------------------------------------------------
   marked mangles TeX ($, _, \\) if it sees it, so math segments are pulled
   out first and swapped for placholder tokens that survive markdown and
   sanitization, then rendered in place with KaTeX afterwards. Code spans
   and fences are skipped so $ inside code stays literal. */

const CODE_RE = /```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]+`/g;
const MATH_ENVS = "align\\*?|equation\\*?|gather\\*?|alignat\\*?|multline\\*?|eqnarray\\*?|aligned|gathered|cases|CD|(?:p|b|v|B|V|small)?matrix";
const MATH_RE = new RegExp(
  "\\\\begin\\{(" + MATH_ENVS + ")\\}[\\s\\S]*?\\\\end\\{\\1\\}" +
  "|\\$\\$[\\s\\S]+?\\$\\$" +
  "|\\\\\\[[\\s\\S]+?\\\\\\]" +
  "|\\\\\\([\\s\\S]+?\\\\\\)" +
  "|(?<![\\\\$\\w])\\$(?!\\s)(?:\\\\[^\\n]|[^$\\\\\\n])+?(?<![\\s\\\\])\\$(?![\\w$])",
  "g");

function extractMath(src) {
  const math = [];
  const grab = (text) => text.replace(MATH_RE, (match) => {
    let tex = match, display = false;
    if (match.startsWith("$$")) { tex = match.slice(2, -2); display = true; }
    else if (match.startsWith("\\[")) { tex = match.slice(2, -2); display = true; }
    else if (match.startsWith("\\(")) { tex = match.slice(2, -2); }
    else if (match.startsWith("\\begin")) { display = true; }
    else { tex = match.slice(1, -1); }
    math.push({ tex: tex.trim(), display });
    return "%%MATH" + (math.length - 1) + "%%";
  });
  let out = "", last = 0;
  for (const m of src.matchAll(CODE_RE)) {
    out += grab(src.slice(last, m.index)) + m[0];
    last = m.index + m[0].length;
  }
  out += grab(src.slice(last));
  return { text: out, math };
}

function renderMarkdown(src) {
  const { text, math } = extractMath(src);
  const box = el("div", "md");
  box.innerHTML = DOMPurify.sanitize(marked.parse(text));
  if (math.length) restoreMath(box, math);
  return box;
}

function restoreMath(container, math) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) {
    if (n.nodeValue.includes("%%MATH")) nodes.push(n);
  }
  for (const node of nodes) {
    if (node.parentElement && node.parentElement.closest("code, pre")) continue;
    const parts = node.nodeValue.split(/%%MATH(\d+)%%/);
    if (parts.length === 1) continue;
    const frag = document.createDocumentFragment();
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 0) {
        if (parts[i]) frag.append(document.createTextNode(parts[i]));
      } else {
        const item = math[Number(parts[i])];
        if (!item) continue;
        const span = el("span");
        try {
          katex.render(item.tex, span, {
            displayMode: item.display, throwOnError: false, strict: "ignore",
          });
        } catch (err) {
          span.textContent = item.tex;
        }
        frag.append(span);
      }
    }
    node.replaceWith(frag);
  }
}

/* ---- equation copy ---------------------------------------------------- */

document.addEventListener("dblclick", (e) => {
  const eq = e.target.closest && e.target.closest(".katex");
  if (!eq) return;
  const ann = eq.querySelector('annotation[encoding="application/x-tex"]');
  if (!ann) return;
  let tex = ann.textContent;
  if (e.shiftKey) {
    tex = eq.closest(".katex-display") ? "$$\n" + tex + "\n$$" : "$" + tex + "$";
  }
  navigator.clipboard.writeText(tex).then(
    () => toast(e.shiftKey ? "Copied TeX with delimiters" : "Copied TeX source"),
    () => toast("Clipboard unavailable"));
  e.preventDefault();
  getSelection().removeAllRanges();
});

let toastTimer = null;
function toast(msg) {
  const node = document.getElementById("toast");
  node.textContent = msg;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 1400);
}

/* ---- search ----------------------------------------------------------- */

async function runSearch(query) {
  const list = document.getElementById("sidebar-list");
  if (!query) { renderProjectList(); return; }
  list.textContent = "Searching…";
  const hits = await api("/api/search?q=" + encodeURIComponent(query));
  list.textContent = "";
  if (!hits.length) {
    list.append(el("div", "empty", "No matches."));
    return;
  }
  for (const h of hits) {
    const row = el("div", "hit");
    const snippet = el("div");
    const idx = h.snippet.toLowerCase().indexOf(query.toLowerCase());
    if (idx >= 0) {
      snippet.append(document.createTextNode("…" + h.snippet.slice(0, idx)));
      snippet.append(el("mark", null, h.snippet.slice(idx, idx + query.length)));
      snippet.append(document.createTextNode(h.snippet.slice(idx + query.length) + "…"));
    } else {
      snippet.textContent = h.snippet;
    }
    row.append(snippet);
    row.append(el("div", "sdate", h.label + " · " + h.role));
    row.addEventListener("click", () =>
      openSession({ dir: h.project, label: h.label }, h.session, null));
    list.append(row);
  }
}
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8483)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--root", default=DEFAULT_ROOT, help="transcript root (default: ~/.claude/projects)"
    )
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        sys.exit(f"transcript root not found: {args.root}")

    Handler.root = args.root
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"serving {args.root} at {url}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
