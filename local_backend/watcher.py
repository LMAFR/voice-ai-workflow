#!/usr/bin/env python3
"""
Local backend: watch the outbox for transcript files and open GitHub issues.

For each new transcript JSON it:
  1. resolves the repo alias via config/repos.json
  2. optionally reformats the transcript into title+body using the Claude CLI and
     a per-repo skill (only if format_enabled and `claude` is on PATH)
  3. creates a GitHub issue via the `gh` CLI
  4. moves the transcript to processed/ (or error/ on failure)

Runs on your local machine. Requires `gh` authenticated (gh auth login).

Usage:
    python watcher.py                 # watch forever
    python watcher.py --once          # process the current backlog and exit
    python watcher.py --config /path/to/repos.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def default_outbox() -> Path:
    env = os.environ.get("VOICE_ISSUE_OUTBOX")
    return Path(env).expanduser() if env else Path.home() / ".voice-issue" / "outbox"


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"error: config not found: {path}\n"
                 f"Copy {ROOT/'config'/'repos.example.json'} to it and edit.")
    return json.loads(path.read_text(encoding="utf-8"))


def have_claude() -> bool:
    return shutil.which("claude") is not None


def format_with_claude(text: str, skill_name: str) -> dict | None:
    """Return {'title', 'body'} or None if formatting is unavailable/failed."""
    skill_path = ROOT / "config" / "skills" / f"{skill_name}.md"
    if not skill_path.exists():
        print(f"  ! skill '{skill_name}' not found, skipping formatting")
        return None
    prompt = (skill_path.read_text(encoding="utf-8")
              + "\n\n--- TRANSCRIPT ---\n" + text + "\n--- END TRANSCRIPT ---\n")
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  ! claude formatting failed ({e}), using raw transcript")
        return None
    if out.returncode != 0:
        print(f"  ! claude exited {out.returncode}, using raw transcript")
        return None
    raw = out.stdout.strip()
    # tolerate accidental ```json fences
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        data = json.loads(raw)
        if "title" in data and "body" in data:
            return {"title": str(data["title"]), "body": str(data["body"])}
    except json.JSONDecodeError:
        pass
    print("  ! could not parse claude output as JSON, using raw transcript")
    return None


def raw_issue(text: str) -> dict:
    """Fallback: first line/sentence as title, full text as body."""
    first = text.strip().splitlines()[0] if text.strip() else "Voice transcript"
    title = (first[:67] + "...") if len(first) > 70 else first
    body = text.strip() + "\n\n_Filed from a voice transcript (unformatted)._"
    return {"title": title, "body": body}


def create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    if os.environ.get("VOICE_ISSUE_DRYRUN"):
        print(f"  [dry-run] would open in {repo}:")
        print(f"           title: {title}")
        print(f"           labels: {labels}")
        print("           body:")
        for line in body.splitlines():
            print(f"             {line}")
        return f"[dry-run] {repo} (not created)"
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for lb in labels or []:
        cmd += ["--label", lb]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        # retry without labels (label may not exist in the repo)
        if labels:
            return create_issue(repo, title, body, [])
        raise RuntimeError(res.stderr.strip() or "gh issue create failed")
    return res.stdout.strip()


def process_file(path: Path, cfg: dict, dirs: dict):
    print(f"→ {path.name}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! unreadable ({e}), moving to error/")
        shutil.move(str(path), dirs["error"] / path.name)
        return

    alias = record.get("repo_alias", "")
    text = record.get("text", "").strip()
    entry = cfg.get("repos", {}).get(alias)
    if not entry:
        print(f"  ! unknown alias '{alias}' (add it to repos.json), moving to error/")
        shutil.move(str(path), dirs["error"] / path.name)
        return
    if not text:
        print("  ! empty transcript, moving to error/")
        shutil.move(str(path), dirs["error"] / path.name)
        return

    repo = entry["repo"]
    labels = entry.get("labels", [])

    issue = None
    if cfg.get("format_enabled") and entry.get("format") and have_claude():
        print(f"  · formatting with claude (skill: {entry['format']})")
        issue = format_with_claude(text, entry["format"])
    if issue is None:
        issue = raw_issue(text)

    try:
        url = create_issue(repo, issue["title"], issue["body"], labels)
        print(f"  ✅ opened issue: {url}")
        shutil.move(str(path), dirs["processed"] / path.name)
    except RuntimeError as e:
        print(f"  ! failed to open issue: {e}")
        shutil.move(str(path), dirs["error"] / path.name)


def scan(outbox: Path, cfg: dict, dirs: dict):
    for f in sorted(outbox.glob("*.json")):
        if f.name.startswith("."):
            continue
        process_file(f, cfg, dirs)


def main():
    ap = argparse.ArgumentParser(description="Watch outbox -> open GitHub issues")
    ap.add_argument("--config", default=str(ROOT / "config" / "repos.json"))
    ap.add_argument("--outbox", default=None)
    ap.add_argument("--once", action="store_true", help="process backlog and exit")
    ap.add_argument("--interval", type=float, default=2.0, help="poll seconds (fallback mode)")
    ap.add_argument("--dry-run", action="store_true", help="log issues instead of creating them")
    args = ap.parse_args()

    if args.dry_run:
        os.environ["VOICE_ISSUE_DRYRUN"] = "1"

    outbox = Path(args.outbox).expanduser() if args.outbox else default_outbox()
    outbox.mkdir(parents=True, exist_ok=True)
    dirs = {
        "processed": outbox.parent / "processed",
        "error": outbox.parent / "error",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    cfg = load_config(Path(args.config).expanduser())
    print(f"watching {outbox}  (config: {args.config}, format_enabled={cfg.get('format_enabled')})")

    if args.once:
        scan(outbox, cfg, dirs)
        return

    # Prefer watchdog for instant pickup; fall back to polling if not installed.
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if event.is_directory or not str(event.src_path).endswith(".json"):
                    return
                time.sleep(0.2)  # let the writer finish the rename
                p = Path(event.src_path)
                if p.exists() and not p.name.startswith("."):
                    process_file(p, cfg, dirs)

        scan(outbox, cfg, dirs)  # clear any backlog first
        obs = Observer()
        obs.schedule(Handler(), str(outbox), recursive=False)
        obs.start()
        print("(watchdog active — Ctrl-C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            obs.stop()
        obs.join()
    except ImportError:
        print("(watchdog not installed — polling every "
              f"{args.interval}s; pip install watchdog for instant pickup)")
        try:
            while True:
                scan(outbox, cfg, dirs)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
