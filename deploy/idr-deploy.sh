#!/usr/bin/env bash
# One-command deploy for the IDR tools. Same file in email_format_tool/deploy/ and
# idr-image-search/deploy/. For each "folder:service:port" it:
#   git pull --ff-only  ->  pip install -r requirements.txt (only if it changed)
#   ->  restart the service  ->  check the tool answers on 127.0.0.1:<port>/login
# and prints one OK/FAILED line per tool (also sent to Slack if SLACK_WEBHOOK_URL is set).
#
# Tools server:  idr-deploy.sh multilang_ui:idr-translate:5000 audio_generation:idr-audio:5001 email_format_tool:idr-emails:5002
# Image server:  idr-deploy.sh idr-image-search:idr-image-search:5000
# (set up as the alias `idr-deploy` - see the README)
set -u

# git pull may replace this very file while it runs; run from a private copy instead.
if [ -z "${IDR_DEPLOY_COPY:-}" ]; then
  tmp=$(mktemp)
  cp "$0" "$tmp"
  IDR_DEPLOY_COPY=1 exec bash "$tmp" "$@"
fi
trap 'rm -f "$0"' EXIT

[ -f "$HOME/idr-tools.env" ] && { set -a; . "$HOME/idr-tools.env"; set +a; }
[ $# -gt 0 ] || { echo "usage: $(basename "$0") folder:service:port ..."; exit 2; }

summary=()
failed=0
for spec in "$@"; do
  IFS=: read -r dir svc port <<< "$spec"
  repo="$HOME/$dir"
  echo "== $svc ($repo)"
  if ! cd "$repo" 2>/dev/null; then
    summary+=("FAILED $svc: folder $repo not found"); failed=1; continue
  fi

  before=$(git rev-parse HEAD)
  if ! git pull --ff-only -q; then
    summary+=("FAILED $svc: git pull failed (local changes? run git status in $repo)"); failed=1; continue
  fi
  after=$(git rev-parse HEAD)
  change=$([ "$before" = "$after" ] && echo "no new code" || echo "$(git log --oneline -1 | cut -c1-60)")

  if [ "$before" != "$after" ] && git diff --name-only "$before" "$after" | grep -qx requirements.txt; then
    py=""
    for v in venv .venv; do [ -x "$repo/$v/bin/pip" ] && py="$repo/$v/bin/pip" && break; done
    if [ -n "$py" ]; then
      echo "   requirements.txt changed - installing"
      "$py" install -q --no-cache-dir -r requirements.txt || { summary+=("FAILED $svc: pip install failed"); failed=1; continue; }
    else
      summary+=("WARNING $svc: requirements.txt changed but no venv found - install by hand")
    fi
  fi

  sudo systemctl restart "$svc"
  ok=""
  for _ in $(seq 1 45); do        # Image Search needs ~30 s to load its model
    sleep 2
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/login" || true)
    [ "${code:-000}" != "000" ] && [ "$code" -lt 500 ] && { ok=1; break; }
  done
  if [ -n "$ok" ] && systemctl is-active -q "$svc"; then
    summary+=("OK $svc - $change")
  else
    summary+=("FAILED $svc: not answering after restart (journalctl -u $svc -n 30)"); failed=1
  fi
done

echo
printf '%s\n' "${summary[@]}"

if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
  icon=$([ $failed = 0 ] && echo ":rocket:" || echo ":warning:")
  text="$icon Deploy on $(hostname): $(printf '%s; ' "${summary[@]}")"
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' \
       --data "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$text")" "$SLACK_WEBHOOK_URL" || true
fi
exit $failed
