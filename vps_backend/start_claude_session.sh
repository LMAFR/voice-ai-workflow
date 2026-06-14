#!/usr/bin/env bash
# Start (or attach to) the tmux session that runs Claude Code for issue handling.
# The poller types prompts into this session, so it must be running and logged in.
set -euo pipefail

SESSION="${1:-claude-issues}"
WORKDIR="${VOICE_ISSUE_WORKDIR:-$HOME/voice-issue-work}"
mkdir -p "$WORKDIR"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "session '$SESSION' already running. Attach with: tmux attach -t $SESSION"
  exit 0
fi

# Start a detached session running Claude Code. --dangerously-skip-permissions
# lets it run git/gh non-interactively; drop that flag if you prefer prompts.
tmux new-session -d -s "$SESSION" -c "$WORKDIR" \
  "claude --dangerously-skip-permissions; bash"
echo "started Claude Code in tmux session '$SESSION' (cwd $WORKDIR)."
echo "Attach to watch it work: tmux attach -t $SESSION"
