# voice-issue

Create and resolve GitHub issues by voice.

Speak a request → it's transcribed locally with Whisper → opened as a GitHub
issue → (optionally) picked up on the VPS by Claude Code, which creates a branch
and opens a PR that closes the issue.

```
┌─────────────── your local machine ───────────────┐      ┌──────── the VPS ────────┐
│                                                   │      │                         │
│  desktop/voice_issue.py   local_backend/watcher.py│      │ vps_backend/            │
│  ┌─────────────────┐      ┌──────────────────────┐│      │  issue_poller.py        │
│  │ record mic      │      │ watch outbox/        ││      │  ┌───────────────────┐  │
│  │ faster-whisper  │ ───▶ │ (opt.) claude format │├─gh──┼─▶│ poll repos for new│  │
│  │ write transcript│ JSON │ gh issue create      ││issue │  │ issues            │  │
│  └─────────────────┘      └──────────────────────┘│      │  │ access ok? ──────┐│  │
│                                                   │      │  └──────────────┐  ││  │
└───────────────────────────────────────────────────┘      │     yes ▼       no▼ ││  │
                                                            │  tmux Claude    email│  │
                                                            │  worktree+PR    +/retry  │
                                                            └─────────────────────────┘
```

## Components

| Path | Runs on | What it does |
|------|---------|--------------|
| `desktop/voice_issue.py` | your machine (Win/Linux/Mac) | Record mic, transcribe with faster-whisper, write a transcript JSON to the outbox. |
| `local_backend/watcher.py` | your machine | Watch the outbox, optionally reformat with a Claude skill, open a GitHub issue via `gh`. |
| `vps_backend/issue_poller.py` | the VPS | Poll repos for new issues, check token access, dispatch to a Claude Code tmux session (worktree → PR), or email you a `/retry` link if access is missing. |
| `vps_backend/issue_api.py` | the VPS | HTTP endpoint so an iPhone "Hey Siri" Shortcut can file an issue by voice — no desktop, no Whisper, no token on the phone. |
| `config/skills/<alias>.md` | your machine | Optional per-repo prompt that formats the transcript into a clean issue. |
| `vps_backend/skills/<owner>__<repo>.md` | the VPS | Optional per-repo guidance Claude reads before making the PR. |

---

## 1. Local setup (your machine)

### Desktop voice app
```bash
cd desktop
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# record a message for the "sunward" repo (press Enter to stop):
python voice_issue.py --repo sunward
# Spanish, if you prefer to force it:
python voice_issue.py --repo sunward --lang es
```
First run downloads the Whisper `medium` model (~1.5 GB). Set a smaller one with
`--model small` (or `VOICE_ISSUE_MODEL=small`) if you want it lighter/faster.
Auto language detection handles English and Spanish; pass `--lang en`/`--lang es`
to force one.

Transcripts land in `~/.voice-issue/outbox/` (override with `VOICE_ISSUE_OUTBOX`).

### Local backend (outbox → GitHub issue)
```bash
# one-time:
gh auth login                                    # authenticate the gh CLI
cp config/repos.example.json config/repos.json   # then edit aliases/repos
pip install -r local_backend/requirements.txt    # watchdog (optional)

# run it (watches forever):
python local_backend/watcher.py
# or process the current backlog once:
python local_backend/watcher.py --once
# preview without creating issues:
python local_backend/watcher.py --once --dry-run
```

`config/repos.json` maps each voice alias to a repo:
```json
{
  "format_enabled": false,
  "repos": {
    "sunward": { "repo": "LMAFR/sunward", "labels": ["voice"] },
    "dance":   { "repo": "LMAFR/dance_analyzer", "labels": ["voice"], "format": "default" }
  }
}
```
Set `"format_enabled": true` to run the optional Claude formatting step (needs the
`claude` CLI on PATH and an active subscription). Each repo's `"format"` names a
prompt file under `config/skills/`. With formatting off, the raw transcript is used.

---

## 2. VPS setup (this server)

```bash
cd vps_backend
cp vps_config.example.json vps_config.json        # then edit repos
# (optional) email alerts — otherwise alerts go to alerts.log:
cp .env.example .env && edit .env                 # Gmail App Password, etc.

# verify access + setup:
python issue_poller.py --check

# start the Claude Code session the poller talks to:
./start_claude_session.sh claude-issues

# run the poller (load .env first if you set up email):
set -a; [ -f .env ] && . ./.env; set +a
python issue_poller.py            # poll forever
python issue_poller.py --once     # single pass
python issue_poller.py --dry-run  # log only, change nothing
```

Run it persistently with tmux/systemd, e.g.:
```bash
tmux new -d -s issue-poller 'cd ~/voice-issue/vps_backend; set -a; . ./.env 2>/dev/null; set +a; python issue_poller.py'
```

### What happens per new issue
1. Poller checks the token can **read + push** the repo (`--check` shows this).
2. **Access OK** → ensures a clone under `workdir`, labels the issue `claude-wip`,
   and types a prompt into the `claude-issues` tmux session asking Claude to make a
   worktree/branch and open a PR that closes the issue.
3. **No access** → emails `ALERT_EMAIL` a link to the issue. Grant the token access,
   then comment **`/retry`** on the issue; the poller re-checks and dispatches.
   (Commenting `/retry` also nudges any stalled task to continue.)

---

## 3. iPhone by voice (Siri Shortcut, no desktop needed)

On iOS you can skip the desktop recorder and Whisper entirely: Apple dictates
on-device (EN/ES), and `vps_backend/issue_api.py` does the alias lookup, optional
Claude formatting, and `gh issue create` on the VPS. The phone holds only a shared
secret — never the GitHub token. The poller then handles the new issue as usual.

### Run the endpoint on the VPS
```bash
cd vps_backend
cp .env.example .env        # set VOICE_ISSUE_API_SECRET (long random string)
cp ../config/repos.example.json ../config/repos.json   # alias → repo map (shared with the watcher)

set -a; . ./.env; set +a
python issue_api.py --dry-run     # smoke test, creates nothing
python issue_api.py               # serve on 127.0.0.1:8787
```
It binds to localhost by design — put a TLS reverse proxy in front (nginx) so the
phone reaches it over HTTPS, e.g. `https://your-host/voice/issue → 127.0.0.1:8787/issue`.
Run it persistently with tmux/systemd like the poller.

It accepts either a raw spoken `phrase` (parsed server-side) or a pre-split
`{repo_alias, text}`. The phrase parser is built for noisy dictation: it drops a
leading "new", tolerates a mis-heard "issue" (e.g. "IU"), strips stray punctuation,
and resolves the alias by exact / number / fuzzy match. Forms understood:
`new <alias> issue <text>` · `new issue <number> <text>` · `<alias>: <text>` · `<alias> <text>`.

- **Numbers** are an unambiguous override: `new issue two <text>` always files to the
  alias mapped under `"numbers"` in repos.json (homophones one/won, two/to/too, three/tree
  are handled). Good when an invented repo name dictates badly.
- **interpret_enabled** (on by default): the endpoint sends the raw dictation to the
  `claude` CLI, which cleans it into a proper title+body and — only if you didn't name a
  repo — picks the best one. A spoken number/word still wins as the repo. If `claude` is
  unavailable it falls back to the deterministic parse, so an issue is still filed.

`GET /health` lists the configured aliases.

### The Shortcut (Shortcuts app)
1. **Text** action holding your secret → set variable `secret`.
2. **Dictate Text** → gives `Dictated Text` (say e.g. *"new sunward issue: the login button does nothing"*).
3. **Get Contents of URL**:
   - URL `https://your-host/voice/issue`, Method **POST**
   - Header `Content-Type: application/json`
   - Request Body **JSON**: `phrase` = Dictated Text, `secret` = the secret variable
4. (optional) **Show Result** of the response so you see the new issue URL.
5. Rename the shortcut, e.g. **"New Issue"**, and run it with *"Hey Siri, new issue"* —
   then speak the `new <alias> issue: …` sentence when it listens.

> Because the alias is spoken inside the dictated sentence, one shortcut covers all
> repos. Say a name (*"new dance issue ..."*) or a number (*"new issue two ..."*); the
> server interprets the rest. (Alternatively make one shortcut per repo with the alias
> hardcoded for a fully hands-free *"Hey Siri, new game issue"*.)

---

## Token permissions (important)

The VPS uses your `gh` token. For the full pipeline the fine-grained PAT needs, on
the target repos:

- **Contents: Read & write** (clone/push branches) ✅ already enabled
- **Pull requests: Read & write** (open PRs)
- **Issues: Read & write** (label issues, and for the local backend to manage them)

> Note: the current token can *create* issues but not *update/close/label* them.
> The poller treats labeling as best-effort (it won't crash), and dedup falls back
> to `state.json`, but widen the PAT for labels + auto-PRs to work cleanly. Repos
> the token can't write trigger the email `/retry` fallback by design.

---

## Adding a third-party repo (a repo you don't own)

The poller serves any repo listed as a plain `"owner/name"` string with the **default
token** (your `GH_TOKEN`). To watch a repo owned by someone else, give that one repo
its own token in `vps_config.json`:

```json
"repos": [
  "LMAFR/sunward",
  { "repo": "someorg/their-repo", "token_env": "THEIR_REPO_TOKEN" }
]
```

`token_env` reads the token from the environment (put it in `.env`). You can also
inline it with `{ "repo": "...", "token": "ghp_..." }` since `vps_config.json` is
gitignored — but `token_env` keeps secrets in one place. `--check` shows which token
serves each repo (`token=default` vs `token=env:THEIR_REPO_TOKEN`).

The default-token path is unchanged: omit any token and the repo behaves exactly as
before.

### REQUIRED security rules for third-party repos

Follow **all** of these whenever you add a repo you don't own:

1. **Get the token from the repo OWNER, scoped to that repo only.** A GitHub
   *fine-grained* PAT can only reach repos owned by the token's own account — yours
   cannot touch their repo even if they add you as a collaborator. Ask the owner to
   mint a fine-grained PAT scoped to **just their one repo**, with **Issues: Read &
   write** and **Pull requests: Read & write** and nothing else. (A classic PAT with
   collaborator access works too but is far broader — avoid it.)
2. **Least privilege.** One repo, Issues + PRs only. Never `Contents`-wide org tokens
   or anything that can reach other repos.
3. **Never commit a token.** `vps_config.json` and `.env` are gitignored; the public
   repo must never receive a token. Per-repo token files the poller writes for the
   Claude session live under `workdir/.tokens/*.token`, chmod **600**, outside the repo.
4. **Keep `.env` at chmod 600.** It holds the tokens.
5. **You are custodian of someone else's write credential.** Treat it accordingly:
   rotate/remove it when the collaboration ends, and prefer the owner revoking it
   on their side.

---

## Files you create (gitignored)
- `config/repos.json` — alias → repo map (local backend)
- `vps_backend/vps_config.json` — repos to watch (VPS)
- `vps_backend/.env` — SMTP creds for email alerts (optional)
- `vps_backend/state.json` — which issues were handled (auto-managed)
