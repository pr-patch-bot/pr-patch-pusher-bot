#!/usr/bin/env bash
set -euo pipefail

: "${CODEBERG_WEBHOOK_SECRET:?set CODEBERG_WEBHOOK_SECRET}"
: "${WEBHOOK_URL:=http://localhost:8888/webhook/codeberg}"
: "${CODEBERG_REPO_FULL_NAME:=montero-project/monero}"
: "${PR_NUMBER:=1}"
: "${ACTION:=opened}"

cat > payload.json <<JSON
{
  "action": "${ACTION}",
  "pull_request": {
    "number": ${PR_NUMBER},
    "base": { "repo": { "full_name": "${CODEBERG_REPO_FULL_NAME}" } }
  }
}
JSON

SIG="$(
  python3 -c 'import hmac,hashlib,sys; s=sys.argv[1].encode(); b=open("payload.json","rb").read(); print(hmac.new(s,b,hashlib.sha256).hexdigest())' \
    "$CODEBERG_WEBHOOK_SECRET"
)"

curl -i \
  -H 'Content-Type: application/json' \
  -H 'X-Gitea-Event: pull_request' \
  -H "X-Gitea-Signature: $SIG" \
  --data-binary @payload.json \
  "$WEBHOOK_URL"

