#!/usr/bin/env python3
"""maude — browse Claude Code transcripts with real math rendering.

Local web app, stdlib only. Serves a small JSON API over
~/.claude/projects/ plus the viewer.html page next to this script, which
renders conversations with marked (markdown) + KaTeX (math). Open
sessions live-update as new records are appended to the transcript.
Double-click any rendered equation to copy its TeX source;
shift-double-click copies it with delimiters.

Usage:
    python3 maude.py                  # serve + open browser
    python3 maude.py --port 9000
    python3 maude.py --root /path/to/projects --no-browser

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


def load_messages(root, project, session, offset=0):
    """Parse records starting at byte `offset` (for live tailing).

    Only whole lines are consumed: a partially-written trailing line stays
    unparsed and the returned offset points at its start, so the next poll
    picks it up once the writer finishes it. `reset` tells the client its
    offset was stale (file truncated/rotated) and the pane must be rebuilt."""
    path = safe_join(root, project, session + ".jsonl")
    reset = False
    if offset and offset > os.path.getsize(path):
        offset = 0
        reset = True
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    end = len(data)
    if data and not data.endswith(b"\n"):
        end = data.rfind(b"\n") + 1  # 0 when the only line is partial
    text = data[:end].decode("utf-8", "replace")

    records = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    messages = []
    for obj in records:
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
    return {"offset": offset + end, "reset": reset, "messages": messages}


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
                self.respond(load_page(), "text/html; charset=utf-8")
            elif url.path == "/api/projects":
                self.send_json(list_projects(self.root))
            elif url.path == "/api/sessions":
                self.send_json(list_sessions(self.root, params["project"]))
            elif url.path == "/api/messages":
                self.send_json(
                    load_messages(
                        self.root,
                        params["project"],
                        params["session"],
                        int(params.get("offset", 0)),
                    )
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
# The page lives in viewer.html next to this script and is re-read on every
# request, so HTML/JS edits show up on refresh without restarting the server.
# --------------------------------------------------------------------------

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer.html")


def load_page():
    with open(HTML_PATH, "rb") as fh:
        return fh.read()


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
