#!/usr/bin/env python3
"""
VPS HTTP endpoint: turn a single spoken phrase into a GitHub issue.

Designed for an iPhone "Hey Siri" Shortcut: the phone dictates one sentence and
POSTs it here; this endpoint parses the repo alias out of the sentence, resolves
it via config/repos.json, optionally reformats it with Claude, and opens the
issue with the VPS's own `gh` token. The phone never holds the GitHub token —
only a shared secret.

The voice-issue poller (issue_poller.py) then picks the new issue up and drives
Claude to a PR, exactly as it does for issues filed from a desktop.

Request (POST /issue, application/json):
    { "phrase": "new sunward issue: the login button does nothing", "secret": "..." }
  or the pre-split form (e.g. if the Shortcut does its own parsing):
    { "repo_alias": "sunward", "text": "the login button does nothing", "secret": "..." }

Response: 200 {"ok": true, "url": "...", "repo": "...", "alias": "..."}
          4xx  {"ok": false, "error": "...", "aliases": [...]}   (on bad input)

Phrase parsing accepts, case-insensitively, any of:
    "new <alias> issue: <text>"     (the recommended Siri structure)
    "new <alias> issue <text>"      (no colon — Siri often drops it)
    "<alias>: <text>"
    "<alias> <text>"                (final fallback: first word is the alias)

Config: config/repos.json (same alias map the local watcher uses).
Auth:   env VOICE_ISSUE_API_SECRET (required). Compared in constant time.
Env:    PORT (default 8787), VOICE_ISSUE_CONFIG (default ../config/repos.json).
        Reads vps_backend/.env if present.

Usage:
    VOICE_ISSUE_API_SECRET=... python issue_api.py            # serve on :8787
    python issue_api.py --dry-run                             # don't create issues
    python issue_api.py --port 9000
"""
import argparse
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

DRY_RUN = False


def load_env():
    """Minimal .env loader (KEY=VALUE lines) so secrets need not be exported."""
    env_path = HERE / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def load_config() -> dict:
    path = Path(os.environ.get("VOICE_ISSUE_CONFIG", ROOT / "config" / "repos.json"))
    if not path.exists():
        sys.exit(f"error: config not found: {path}\n"
                 f"Copy {ROOT/'config'/'repos.example.json'} to it and edit.")
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- phrase parse
# Patterns are tried in order; the first that matches wins. All case-insensitive,
# DOTALL so multi-sentence dictation flows into the body.
_PHRASE_PATTERNS = [
    re.compile(r"^\s*(?:new\s+)?(?P<alias>[\w-]+)\s+issue\s*[:\-]\s*(?P<text>.+)$", re.I | re.S),
    re.compile(r"^\s*(?:new\s+)?(?P<alias>[\w-]+)\s+issue\s+(?P<text>.+)$", re.I | re.S),
    re.compile(r"^\s*(?P<alias>[\w-]+)\s*[:\-]\s*(?P<text>.+)$", re.I | re.S),
    re.compile(r"^\s*(?P<alias>[\w-]+)\s+(?P<text>.+)$", re.I | re.S),
]


def parse_phrase(phrase: str) -> tuple[str, str] | None:
    """Pull (alias, text) out of a spoken sentence, or None if unparseable."""
    phrase = phrase.strip()
    for pat in _PHRASE_PATTERNS:
        m = pat.match(phrase)
        if m:
            return m.group("alias").strip(), m.group("text").strip()
    return None


# --------------------------------------------------------------- issue creation
def have_claude() -> bool:
    return shutil.which("claude") is not None


def format_with_claude(text: str, skill_name: str) -> dict | None:
    """Return {'title', 'body'} or None if formatting is unavailable/failed."""
    skill_path = ROOT / "config" / "skills" / f"{skill_name}.md"
    if not skill_path.exists():
        return None
    prompt = (skill_path.read_text(encoding="utf-8")
              + "\n\n--- TRANSCRIPT ---\n" + text + "\n--- END TRANSCRIPT ---\n")
    try:
        out = subprocess.run(["claude", "-p", prompt, "--output-format", "text"],
                             capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    raw = out.stdout.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        data = json.loads(raw)
        if "title" in data and "body" in data:
            return {"title": str(data["title"]), "body": str(data["body"])}
    except json.JSONDecodeError:
        pass
    return None


def raw_issue(text: str) -> dict:
    first = text.strip().splitlines()[0] if text.strip() else "Voice transcript"
    title = (first[:67] + "...") if len(first) > 70 else first
    body = text.strip() + "\n\n_Filed by voice from iPhone (Siri Shortcut)._"
    return {"title": title, "body": body}


def create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    if DRY_RUN:
        return f"[dry-run] {repo} :: {title}"
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for lb in labels or []:
        cmd += ["--label", lb]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        if labels:  # label may not exist in the repo — retry without
            return create_issue(repo, title, body, [])
        raise RuntimeError(res.stderr.strip() or "gh issue create failed")
    return res.stdout.strip()


def handle_issue(payload: dict, cfg: dict) -> tuple[int, dict]:
    """Core logic, separated from HTTP so it is unit-testable. Returns (status, body)."""
    aliases = sorted(cfg.get("repos", {}).keys())

    # Resolve alias + text from either the pre-split fields or a raw phrase.
    alias = (payload.get("repo_alias") or "").strip()
    text = (payload.get("text") or "").strip()
    if not (alias and text):
        parsed = parse_phrase(payload.get("phrase") or "")
        if not parsed:
            return 400, {"ok": False, "error": "could not parse phrase; "
                         "say 'new <alias> issue: <what to file>'", "aliases": aliases}
        alias, text = parsed

    entry = cfg.get("repos", {}).get(alias.lower()) or cfg.get("repos", {}).get(alias)
    if not entry:
        return 404, {"ok": False, "error": f"unknown alias '{alias}'", "aliases": aliases}
    if not text:
        return 400, {"ok": False, "error": "empty issue text", "aliases": aliases}

    repo = entry["repo"]
    labels = entry.get("labels", [])

    issue = None
    if cfg.get("format_enabled") and entry.get("format") and have_claude():
        issue = format_with_claude(text, entry["format"])
    if issue is None:
        issue = raw_issue(text)

    try:
        url = create_issue(repo, issue["title"], issue["body"], labels)
    except RuntimeError as e:
        return 502, {"ok": False, "error": f"gh issue create failed: {e}"}
    return 200, {"ok": True, "url": url, "repo": repo, "alias": alias, "title": issue["title"]}


# ------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    cfg: dict = {}
    secret: str = ""

    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quieter, single-line logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "aliases": sorted(self.cfg.get("repos", {}).keys())})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _read_body(self) -> dict | None:
        """Accept either a JSON or a form-urlencoded body (iOS Shortcuts can send
        whichever — "JSON" vs "Form" in Get Contents of URL). Returns a dict or None."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        text = raw.decode("utf-8", "replace").strip()
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype == "application/json" or text[:1] in "{[":
            try:
                data = json.loads(text)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        # form-urlencoded (phrase=...&secret=...)
        parsed = parse_qs(text, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()} if parsed else None

    def do_POST(self):
        if self.path != "/issue":
            return self._send(404, {"ok": False, "error": "not found"})
        payload = self._read_body()
        if payload is None:
            return self._send(400, {"ok": False,
                                    "error": "body must be JSON or form-encoded with phrase + secret"})

        given = str(payload.get("secret", ""))
        if not self.secret or not hmac.compare_digest(given, self.secret):
            return self._send(401, {"ok": False, "error": "bad or missing secret"})

        status, body = handle_issue(payload, self.cfg)
        self._send(status, body)


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser(description="Voice -> GitHub issue HTTP endpoint")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1; put a TLS proxy in front)")
    ap.add_argument("--dry-run", action="store_true", help="don't create issues, just echo")
    args = ap.parse_args()
    DRY_RUN = args.dry_run

    load_env()
    Handler.secret = os.environ.get("VOICE_ISSUE_API_SECRET", "")
    if not Handler.secret and not DRY_RUN:
        sys.exit("error: set VOICE_ISSUE_API_SECRET (env or vps_backend/.env)")
    Handler.cfg = load_config()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    aliases = ", ".join(sorted(Handler.cfg.get("repos", {}).keys())) or "(none)"
    print(f"voice-issue API on http://{args.host}:{args.port}  "
          f"aliases: {aliases}  dry_run={DRY_RUN}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
