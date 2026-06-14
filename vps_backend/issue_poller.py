#!/usr/bin/env python3
"""
VPS backend: watch configured GitHub repos for newly-opened issues and hand them
to a running Claude Code tmux session to produce a PR.

Flow per new open issue (not already labeled `claude-wip`/`claude-done`):
  1. Verify the VPS token can READ and WRITE (push) the repo.
  2. If yes  -> ensure a local clone exists, label the issue `claude-wip`, and
                send a one-line prompt to the Claude Code tmux session asking it
                to create a worktree+branch and open a PR that closes the issue.
  3. If no   -> email the alert address a link to the issue with instructions:
                grant the token access, then comment `/retry` on the issue.
                The issue is recorded as `awaiting_access`.

Resume: any `awaiting_access` issue that receives a NEW `/retry` comment from the
repo owner is re-checked; if access is now granted it is dispatched. This also
lets you nudge an in-progress task ("continue ...") by commenting `/retry`.

Usage:
    python issue_poller.py --check        # one-shot access check for all repos
    python issue_poller.py --once         # one polling pass, then exit
    python issue_poller.py                # poll forever (default 60s)
    python issue_poller.py --dry-run      # don't dispatch/label/email, just log

Config: vps_backend/vps_config.json  (see vps_config.example.json)
Email:  via env SMTP_HOST/PORT/USER/PASS/FROM + ALERT_EMAIL (see .env.example).
        If SMTP is not configured, alerts are appended to alerts.log instead.
"""
import argparse
import datetime as dt
import json
import os
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
ALERTS_LOG = HERE / "alerts.log"
WIP_LABEL = "claude-wip"
DONE_LABEL = "claude-done"

DRY_RUN = False


def log(msg: str):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------- gh
def gh_env(token: str | None):
    """Environment for a gh/git call. None -> inherit (the default GH_TOKEN already
    in the process env is used). A token -> override GH_TOKEN/GITHUB_TOKEN for that
    one call, so a repo can be served by a token other than our default."""
    if not token:
        return None
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    return env


def gh_json(args: list[str], token: str | None = None):
    res = subprocess.run(["gh", *args], capture_output=True, text=True, env=gh_env(token))
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"gh {' '.join(args)} failed")
    return json.loads(res.stdout) if res.stdout.strip() else None


def gh_run(args: list[str], check=True, token: str | None = None):
    res = subprocess.run(["gh", *args], capture_output=True, text=True, env=gh_env(token))
    if check and res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"gh {' '.join(args)} failed")
    return res


def authed_login(token: str | None = None) -> str:
    # --jq on a bare string yields unquoted text, so read it raw rather than as JSON.
    res = gh_run(["api", "user", "--jq", ".login"], check=False, token=token)
    return res.stdout.strip() if res.returncode == 0 else ""


_login_cache: dict[str, str] = {}


def login_for(token: str | None) -> str:
    """The GitHub login the given token authenticates as (cached). For our default
    token this is the account that owns it; for a third-party token it is whoever
    minted it — which is exactly who would grant access and comment /retry."""
    key = token or "__default__"
    if key not in _login_cache:
        _login_cache[key] = authed_login(token)
    return _login_cache[key]


def repo_access(repo: str, token: str | None = None) -> dict:
    """Return {'read': bool, 'write': bool, 'error': str|None}."""
    try:
        perms = gh_json(["api", f"repos/{repo}", "--jq", "{push: .permissions.push, pull: .permissions.pull}"], token=token)
    except RuntimeError as e:
        return {"read": False, "write": False, "error": str(e)}
    return {"read": bool(perms.get("pull")), "write": bool(perms.get("push")), "error": None}


def list_open_issues(repo: str, token: str | None = None) -> list[dict]:
    # exclude PRs (gh issue list already excludes them)
    return gh_json([
        "issue", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,url,labels,updatedAt", "--limit", "50",
    ], token=token) or []


def list_comments(repo: str, number: int, token: str | None = None) -> list[dict]:
    data = gh_json(["api", f"repos/{repo}/issues/{number}/comments",
                    "--jq", "[.[] | {id, body, login: .user.login}]"], token=token)
    return data or []


def ensure_label(repo: str, label: str, token: str | None = None):
    if DRY_RUN:
        return
    gh_run(["label", "create", label, "--repo", repo, "--force",
            "--color", "5319e7", "--description", "handled by voice-issue VPS backend"],
           check=False, token=token)


def add_label(repo: str, number: int, label: str, token: str | None = None):
    if DRY_RUN:
        log(f"  [dry-run] would label #{number} '{label}'")
        return
    ensure_label(repo, label, token)
    gh_run(["issue", "edit", str(number), "--repo", repo, "--add-label", label], check=False, token=token)


# ---------------------------------------------------------------------- clone
def repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def ensure_clone(repo: str, workdir: Path, token: str | None = None) -> Path:
    dest = workdir / repo_slug(repo)
    if dest.exists():
        subprocess.run(["git", "-C", str(dest), "fetch", "--all", "--prune"],
                       capture_output=True, text=True, env=gh_env(token))
        return dest
    workdir.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(["gh", "repo", "clone", repo, str(dest)],
                         capture_output=True, text=True, env=gh_env(token))
    if res.returncode != 0:
        raise RuntimeError(f"clone failed: {res.stderr.strip()}")
    return dest


def write_token_file(workdir: Path, repo: str, token: str) -> Path:
    """Persist a repo's token to a 0600 file so the Claude session can source it
    for its own git/gh commands without the secret ever appearing in the tmux
    prompt (and thus the scrollback/logs)."""
    tdir = workdir / ".tokens"
    tdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(tdir, 0o700)
    except OSError:
        pass
    path = tdir / f"{repo_slug(repo)}.token"
    path.write_text(token, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# ----------------------------------------------------------------------- tmux
def tmux_session_exists(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0


def dispatch_to_claude(session: str, repo: str, number: int, url: str, clone_dir: Path,
                       token_file: Path | None = None):
    # Optional per-repo skill/agent instructions: vps_backend/skills/<owner__name>.md
    skill = HERE / "skills" / f"{repo_slug(repo)}.md"
    skill_clause = (
        f"Before starting, read the repo-specific instructions in {skill}. "
        if skill.exists() else ""
    )
    # Repos served by a non-default token: the Claude session must use THAT token for
    # all git/gh commands, otherwise it would push with our default token and 403.
    # The token is read from a 0600 file rather than passed inline so it never lands
    # in the tmux prompt/scrollback.
    token_clause = (
        f"IMPORTANT: this repo requires a specific GitHub token. Before running ANY git "
        f"or gh command for it, in the same shell run: "
        f'export GH_TOKEN="$(cat {token_file})"; export GITHUB_TOKEN="$GH_TOKEN". '
        f"Never print, echo, or paste the token itself. "
        if token_file else ""
    )
    prompt = (
        f"Please resolve GitHub issue {url} (repo {repo}). "
        f"{token_clause}"
        f"First read it: `gh issue view {number} --repo {repo}`. "
        f"{skill_clause}"
        f"A clone of the repo is at {clone_dir}. "
        f"From there create a NEW git worktree and branch named issue-{number} off the "
        f"default branch, implement a solution, commit, push the branch, and open a pull "
        f"request whose description contains 'Closes #{number}'. "
        f"If you cannot push/clone due to permissions, reply exactly ACCESS_DENIED and stop."
    )
    if DRY_RUN:
        log(f"  [dry-run] would send to tmux '{session}': {prompt[:80]}...")
        return True
    if not tmux_session_exists(session):
        log(f"  ! tmux session '{session}' not found — start it (see start_claude_session.sh)")
        return False
    # send literally, then Enter to submit
    subprocess.run(["tmux", "send-keys", "-t", session, "-l", "--", prompt], check=False)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=False)
    return True


# ---------------------------------------------------------------------- email
def send_alert(repo: str, number: int, url: str, reason: str):
    alert_to = os.environ.get("ALERT_EMAIL", "alejandrofloridoreyes@gmail.com")
    subject = f"[voice-issue] No access to {repo} for issue #{number}"
    body = (
        f"Claude Code on the VPS could not act on a new GitHub issue because the "
        f"VPS token lacks access.\n\n"
        f"Repo:   {repo}\n"
        f"Issue:  #{number}\n"
        f"Link:   {url}\n"
        f"Reason: {reason}\n\n"
        f"To resume:\n"
        f"  1. Grant the VPS token read+write access to {repo}.\n"
        f"  2. Open the issue ({url}) and add a comment containing: /retry\n\n"
        f"The poller will detect the /retry comment, re-check access, and dispatch the "
        f"task to Claude Code automatically. You can also comment /retry to nudge a task "
        f"that stalled for any reason.\n"
    )
    host = os.environ.get("SMTP_HOST")
    if not host:
        with ALERTS_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {dt.datetime.now().isoformat()} ===\nTO: {alert_to}\n{subject}\n{body}\n")
        log(f"  ! SMTP not configured — alert written to {ALERTS_LOG.name} (would email {alert_to})")
        return
    if DRY_RUN:
        log(f"  [dry-run] would email {alert_to}: {subject}")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", alert_to))
    msg["To"] = alert_to
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            s.send_message(msg)
        log(f"  ✉  emailed access alert to {alert_to}")
    except Exception as e:  # noqa: BLE001 — never let email failure crash the poller
        with ALERTS_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {dt.datetime.now().isoformat()} (SMTP FAILED: {e}) ===\nTO: {alert_to}\n{subject}\n{body}\n")
        log(f"  ! email send failed ({e}) — alert written to {ALERTS_LOG.name}")


# ---------------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"repos": {}}


def save_state(state: dict):
    if DRY_RUN:
        return
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def issue_state(state: dict, repo: str, number: int) -> dict:
    return state["repos"].setdefault(repo, {}).setdefault("issues", {}).get(str(number), {})


def set_issue_state(state: dict, repo: str, number: int, **kw):
    issues = state["repos"].setdefault(repo, {}).setdefault("issues", {})
    issues.setdefault(str(number), {}).update(kw)


# ----------------------------------------------------------------------- core
def try_dispatch(entry: dict, issue: dict, cfg: dict, state: dict) -> str:
    repo, token = entry["repo"], entry.get("token")
    owner_login = login_for(token)
    number, url = issue["number"], issue["url"]
    acc = repo_access(repo, token)
    if not acc["read"] or not acc["write"]:
        reason = acc["error"] or (
            "token has read but not write (push)" if acc["read"] else "token cannot read the repo"
        )
        log(f"  no access to {repo} ({reason}) -> emailing alert")
        send_alert(repo, number, url, reason)
        set_issue_state(state, repo, number, status="awaiting_access", last_retry_comment=last_retry_id(repo, number, owner_login, token))
        return "awaiting_access"
    workdir = Path(cfg["workdir"]).expanduser()
    try:
        clone_dir = ensure_clone(repo, workdir, token)
    except RuntimeError as e:
        log(f"  clone failed ({e}) -> emailing alert")
        send_alert(repo, number, url, str(e))
        set_issue_state(state, repo, number, status="awaiting_access")
        return "awaiting_access"
    token_file = write_token_file(workdir, repo, token) if token else None
    ok = dispatch_to_claude(cfg["tmux_session"], repo, number, url, clone_dir, token_file)
    if ok:
        add_label(repo, number, WIP_LABEL, token)
        set_issue_state(state, repo, number, status="dispatched")
        log(f"  ✅ dispatched #{number} to Claude session '{cfg['tmux_session']}'")
        return "dispatched"
    set_issue_state(state, repo, number, status="pending_session")
    return "pending_session"


def last_retry_id(repo: str, number: int, owner_login: str, token: str | None = None) -> int:
    """Highest comment id from the owner that contains /retry (0 if none)."""
    try:
        comments = list_comments(repo, number, token)
    except RuntimeError:
        return 0
    ids = [c["id"] for c in comments
           if "/retry" in (c.get("body") or "").lower()
           and (not owner_login or c.get("login") == owner_login)]
    return max(ids) if ids else 0


def poll_once(cfg: dict, state: dict):
    for entry in cfg["repos"]:
        repo, token = entry["repo"], entry.get("token")
        owner_login = login_for(token)
        try:
            issues = list_open_issues(repo, token)
        except RuntimeError as e:
            log(f"! cannot list issues for {repo}: {e}")
            continue
        for issue in issues:
            number = issue["number"]
            labels = {l["name"] for l in issue.get("labels", [])}
            if DONE_LABEL in labels:
                continue
            st = issue_state(state, repo, number)
            status = st.get("status")

            if status == "awaiting_access":
                newest = last_retry_id(repo, number, owner_login, token)
                if newest and newest != st.get("last_retry_comment", 0):
                    log(f"{repo}#{number}: new /retry detected -> re-checking access")
                    try_dispatch(entry, issue, cfg, state)
                continue

            if status in ("dispatched", "pending_session"):
                if status == "pending_session":  # session was down before; try again
                    log(f"{repo}#{number}: retrying dispatch (session may be up now)")
                    try_dispatch(entry, issue, cfg, state)
                continue

            if WIP_LABEL in labels:
                # labeled by a previous run/instance — record and skip
                set_issue_state(state, repo, number, status="dispatched")
                continue

            # brand-new issue
            log(f"{repo}#{number}: new open issue '{issue['title']}'")
            try_dispatch(entry, issue, cfg, state)
    save_state(state)


def cmd_check(cfg: dict):
    print("Access check (token can read + push?):")
    for entry in cfg["repos"]:
        repo, token = entry["repo"], entry.get("token")
        acc = repo_access(repo, token)
        mark = "✅" if (acc["read"] and acc["write"]) else "❌"
        detail = acc["error"] or f"read={acc['read']} write={acc['write']}"
        src = f"token={entry['token_src']}" if entry.get("token_src") else "token=default"
        print(f"  {mark} {repo}: {detail} [{src}]")
    sess = cfg.get("tmux_session", "claude-issues")
    print(f"tmux session '{sess}': {'present' if tmux_session_exists(sess) else 'NOT running'}")
    print(f"SMTP configured: {'yes' if os.environ.get('SMTP_HOST') else 'no (alerts -> alerts.log)'}")


def normalize_repos(raw: list) -> list[dict]:
    """Accept either 'owner/name' strings (served by the default token) or objects
    {repo, token | token_env} for repos needing a different token. Returns a list of
    {repo, token, token_src} dicts; token is None for default-token repos."""
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"repo": item, "token": None, "token_src": None})
            continue
        if not isinstance(item, dict) or not item.get("repo"):
            sys.exit(f"error: invalid repo entry (need a string or {{'repo': ...}}): {item!r}")
        repo = item["repo"]
        token, src = None, None
        if item.get("token_env"):
            src = f"env:{item['token_env']}"
            token = os.environ.get(item["token_env"])
            if not token:
                sys.exit(f"error: token_env '{item['token_env']}' for repo {repo} "
                         f"is not set in the environment")
        elif item.get("token"):
            src = "inline"
            token = item["token"]
        out.append({"repo": repo, "token": token, "token_src": src})
    return out


def load_cfg(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"error: config not found: {path}\n"
                 f"Copy {HERE/'vps_config.example.json'} to it and edit.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("tmux_session", "claude-issues")
    cfg.setdefault("workdir", str(Path.home() / "voice-issue-work"))
    cfg.setdefault("poll_interval", 60)
    if not cfg.get("repos"):
        sys.exit("error: config has no 'repos' list")
    cfg["repos"] = normalize_repos(cfg["repos"])
    return cfg


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser(description="Poll GitHub issues -> Claude tmux PRs")
    ap.add_argument("--config", default=str(HERE / "vps_config.json"))
    ap.add_argument("--check", action="store_true", help="one-shot access/setup check")
    ap.add_argument("--once", action="store_true", help="single polling pass then exit")
    ap.add_argument("--dry-run", action="store_true", help="log actions, change nothing")
    args = ap.parse_args()
    DRY_RUN = args.dry_run

    cfg = load_cfg(Path(args.config).expanduser())

    if args.check:
        cmd_check(cfg)
        return

    state = load_state()
    repo_names = [e["repo"] for e in cfg["repos"]]  # never log tokens
    log(f"poller starting (session='{cfg['tmux_session']}', repos={repo_names}, "
        f"interval={cfg['poll_interval']}s, default_owner='{login_for(None)}', dry_run={DRY_RUN})")

    if args.once:
        poll_once(cfg, state)
        return
    try:
        while True:
            poll_once(cfg, state)
            time.sleep(cfg["poll_interval"])
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
