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
import difflib
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
# Dictation is noisy: it mis-hears "issue" as "IU", wraps words in parens, and
# garbles invented names. So we tokenize rather than regex-match, drop leading
# filler + a stray "issue"-ish word, then resolve the alias by exact / number /
# fuzzy match. A spoken number ("new issue two ...") is an unambiguous override.
FILLER_LEAD = {"new", "a", "an", "the"}
ISSUE_WORDS = {"issue", "issues", "iu", "ishu", "ish"}
# spoken-number homophones -> canonical key looked up in config["numbers"]
NUMBER_WORDS = {
    "one": "one", "won": "one", "1": "one",
    "two": "two", "to": "two", "too": "two", "2": "two",
    "three": "three", "tree": "three", "free": "three", "3": "three",
}


def _norm(tok: str) -> str:
    return re.sub(r"[^a-z0-9]", "", tok.lower())


def resolve_alias(token: str, cfg: dict) -> str | None:
    """Map a spoken/typed token to a canonical repo alias key, via exact match,
    the number map (one/two/three + homophones), or fuzzy match. None if no hit."""
    t = _norm(token)
    if not t:
        return None
    repos = cfg.get("repos", {})
    if t in repos:
        return t
    n = NUMBER_WORDS.get(t)
    numbers = cfg.get("numbers", {})
    if n and n in numbers and numbers[n] in repos:
        return numbers[n]
    m = difflib.get_close_matches(t, list(repos.keys()), n=1, cutoff=0.8)
    return m[0] if m else None


def _is_issue_word(tok: str) -> bool:
    t = _norm(tok)
    return t in ISSUE_WORDS or bool(difflib.get_close_matches(t, ["issue"], n=1, cutoff=0.7))


def parse_phrase(phrase: str, cfg: dict) -> tuple[str | None, str]:
    """From a (possibly garbled) dictated sentence return (resolved_alias|None, body).
    Handles 'new <alias> issue <body>', 'new issue <number> <body>', '<alias>: <body>',
    stray punctuation/casing, and a mis-heard 'issue' (e.g. 'IU')."""
    words = re.split(r"\s+", (phrase or "").strip())
    words = [w for w in words if w]
    if not words:
        return None, ""
    # drop a leading filler word ("new")
    i = 0
    while i < len(words) and _norm(words[i]) in FILLER_LEAD:
        i += 1
    # drop one "issue"-ish word among the first couple of remaining tokens
    cleaned: list[str] = []
    dropped_issue = False
    for w in words[i:]:
        if not dropped_issue and len(cleaned) < 2 and _is_issue_word(w):
            dropped_issue = True
            continue
        cleaned.append(w)
    if not cleaned:
        return None, ""
    alias = resolve_alias(cleaned[0], cfg)
    if alias:
        body = " ".join(cleaned[1:]).strip().strip(":-").strip()
        return alias, body
    # first token isn't a known alias — leave alias unresolved, keep the whole remainder
    return None, " ".join(cleaned).strip()


# --------------------------------------------------------------- issue creation
def have_claude() -> bool:
    return shutil.which("claude") is not None


def _run_claude_json(prompt: str) -> dict | None:
    """Run `claude -p` and parse its reply as a JSON object. None on any failure."""
    try:
        out = subprocess.run(["claude", "-p", prompt, "--output-format", "text"],
                             capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    raw = out.stdout.strip()
    if "{" in raw and "}" in raw:  # tolerate ```json fences / surrounding prose
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def format_with_claude(text: str, skill_name: str) -> dict | None:
    """Return {'title', 'body'} or None if formatting is unavailable/failed."""
    skill_path = ROOT / "config" / "skills" / f"{skill_name}.md"
    if not skill_path.exists():
        return None
    prompt = (skill_path.read_text(encoding="utf-8")
              + "\n\n--- TRANSCRIPT ---\n" + text + "\n--- END TRANSCRIPT ---\n")
    data = _run_claude_json(prompt)
    if data and "title" in data and "body" in data:
        return {"title": str(data["title"]), "body": str(data["body"])}
    return None


def interpret_with_claude(phrase: str, cfg: dict, fixed_alias: str | None) -> dict | None:
    """Use Claude to clean a noisy dictation into a GitHub issue, and (only when the
    repo isn't already known) to pick the target repo. Returns {alias,title,body}."""
    repos = cfg.get("repos", {})
    lines, seen = [], set()
    for alias, entry in repos.items():
        if entry["repo"] in seen:
            continue
        seen.add(entry["repo"])
        lines.append(f"  {alias}  ->  {entry['repo']}")
    repo_list = "\n".join(lines)
    if fixed_alias:
        repo_instr = (f'The target repo alias is ALREADY chosen: "{fixed_alias}". '
                      'Return it verbatim as "alias"; do not change it.')
    else:
        repo_instr = ('Pick the SINGLE best alias from the list above for where this belongs. '
                      'If you genuinely cannot tell, use "unknown".')
    prompt = (
        "You convert a voice-dictated (often mis-transcribed) phrase into a GitHub issue.\n"
        "Available repo aliases:\n" + repo_list + "\n\n" + repo_instr + "\n"
        "Dictation mis-hears words (e.g. 'issue'->'IU'); infer the real intent and write a "
        "clear, concise issue. Stay faithful — don't invent requirements that aren't implied.\n"
        'Return ONLY minified JSON: {"alias":"...","title":"...","body":"..."}\n\n'
        "Dictation:\n" + phrase + "\n"
    )
    data = _run_claude_json(prompt)
    if data and data.get("title") and data.get("body"):
        return {"alias": str(data.get("alias", "")),
                "title": str(data["title"]), "body": str(data["body"])}
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
    """Core logic, separated from HTTP so it is unit-testable. Returns (status, body).

    Pipeline: (1) deterministically pull a repo alias + body from the request — a
    spoken number ("new issue two ...") or a clear word is an unambiguous override;
    (2) when `interpret_enabled`, ask Claude to clean the (noisy) text into a proper
    issue, and to pick the repo only if step 1 couldn't; (3) fall back to a per-repo
    format skill, else the raw text."""
    repos = cfg.get("repos", {})
    aliases = sorted(repos.keys())

    explicit = (payload.get("repo_alias") or "").strip()
    text = (payload.get("text") or "").strip()
    if explicit and text:
        det_alias = resolve_alias(explicit, cfg)
        body_text = text
        raw_for_claude = text
    else:
        raw_for_claude = (payload.get("phrase") or "").strip()
        det_alias, body_text = parse_phrase(raw_for_claude, cfg)

    if not raw_for_claude and not body_text:
        return 400, {"ok": False, "error": "empty request — nothing dictated", "aliases": aliases}

    # Optional smart interpretation: clean the text and, if needed, choose the repo.
    interp = None
    if cfg.get("interpret_enabled") and have_claude() and raw_for_claude:
        interp = interpret_with_claude(raw_for_claude, cfg, fixed_alias=det_alias)

    final_alias = det_alias or (resolve_alias(interp["alias"], cfg) if interp else None)
    if not final_alias:
        return 404, {"ok": False, "aliases": aliases,
                     "error": "couldn't tell which repo — say e.g. 'new dance issue ...' "
                              "or use a number 'new issue two ...'"}

    entry = repos[final_alias]
    repo = entry["repo"]
    labels = entry.get("labels", [])

    if interp:
        issue = {"title": interp["title"], "body": interp["body"]}
    elif cfg.get("format_enabled") and entry.get("format") and have_claude():
        issue = format_with_claude(body_text or raw_for_claude, entry["format"]) \
            or raw_issue(body_text or raw_for_claude)
    else:
        if not (body_text or raw_for_claude):
            return 400, {"ok": False, "error": "empty issue text", "aliases": aliases}
        issue = raw_issue(body_text or raw_for_claude)

    try:
        url = create_issue(repo, issue["title"], issue["body"], labels)
    except RuntimeError as e:
        return 502, {"ok": False, "error": f"gh issue create failed: {e}"}
    return 200, {"ok": True, "url": url, "repo": repo, "alias": final_alias,
                 "title": issue["title"], "interpreted": bool(interp)}


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
