#!/bin/sh
set -e

if [ -n "$VAULT_SECRETS" ] ; then

  echo "⏳ Vault pre-check..."

  TIMEOUT=30
  COUNT=0
  READY=false

  while [ $COUNT -lt $TIMEOUT ] && [ "$READY" = false ] ; do
    sleep 2
    COUNT=$((COUNT+1))
    READY=true
    for e in $VAULT_SECRETS; do
      if [ ! -f "/secrets/$e.env" ]; then
        READY=false
      fi
    done
  done

  echo "📂 Loading env : $VAULT_SECRETS"
  set -a
  for e in $VAULT_SECRETS; do
    if [ -f "/secrets/$e.env" ]; then
        echo "📂 Loading $e.env"
        . "/secrets/$e.env"
    else
        echo "⚠️ Missing $e.env"
    fi
  done
  set +a

else
  echo "⚠️ Vault not configured, skipping"
fi

echo "🚀 Launching app: $@"

# 🔥 SAFE EXEC
if [ $# -eq 0 ]; then
  echo "❌ No command provided, exiting"
  exit 1
fi

exec "$@"