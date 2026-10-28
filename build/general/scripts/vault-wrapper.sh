#!/bin/sh
set -e

if [ -z "$VAULT_ROLE_ID" ] || [ -z "$VAULT_SECRET_ID" ]; then
    VAULT_ROLE_ID="$VAULT_ROOT_ROLE_ID"
    VAULT_SECRET_ID="$VAULT_ROOT_SECRET_ID"
    unset VAULT_ROOT_ROLE_ID VAULT_ROOT_SECRET_ID
fi

if [ -z "$VAULT_ROLE_ID" ] || [ -z "$VAULT_SECRET_ID" ]; then
    VAULT_ROLE_ID="$VAULT_APP_ROLE_ID"
    VAULT_SECRET_ID="$VAULT_APP_SECRET_ID"
    unset VAULT_APP_ROLE_ID VAULT_APP_SECRET_ID
fi

###############################################################################################################
# Rétrocompatibilité pour les machines non mise à jours : à supprimer une fois toutes les machines mise à jours
if [ -z "$VAULT_ROLE_ID" ] || [ -z "$VAULT_SECRET_ID" ]; then
  echo "⚠️ Nouvelle version Vault-agent à mettre en place !"
  SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
  "$SCRIPT_DIR/vault-wrapper-old.sh" "$@"
fi
# Rétrocompatibilité pour les machines non mise à jours : à supprimer une fois toutes les machines mise à jours
###############################################################################################################



VAULT_ADDR="http://vault:8200"
export DEBIAN_FRONTEND=noninteractive
export TERM=dumb
CURL_INSTALLED=false
JQ_INSTALLED=false
SKIP_INSTALL=${SKIP_INSTALL:-false}

add_tools() {
    tool="$1"

    if command -v "$tool" >/dev/null 2>&1; then
        if [ "$tool" = "curl" ]; then
            CURL_INSTALLED=true
        elif [ "$tool" = "jq" ]; then
            JQ_INSTALLED=true
        fi
        return
    fi

    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq >/dev/null &&
        apt-get install -y -qq "$tool" >/dev/null
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache "$tool" >/dev/null
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y "$tool" >/dev/null
    elif command -v yum >/dev/null 2>&1; then
        yum install -y "$tool" >/dev/null
    elif command -v microdnf >/dev/null 2>&1; then
        microdnf install -y "$tool" >/dev/null
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm "$tool" >/dev/null
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install "$tool" >/dev/null
    else
        echo "❌ Aucun gestionnaire de paquets compatible trouvé"
        exit 1
    fi
}

remove_tools() {
    tool="$1"

    if [ "$tool" = "curl" ]; then
        was_installed="$CURL_INSTALLED"
    elif [ "$tool" = "jq" ]; then
        was_installed="$JQ_INSTALLED"
    fi

    if [ "$was_installed" = false ]; then
        if command -v apt-get >/dev/null 2>&1; then
            apt-get purge -y -qq "$tool" >/dev/null
            apt-get autoremove -y -qq >/dev/null
        elif command -v apk >/dev/null 2>&1; then
            apk del "$tool" >/dev/null
        elif command -v dnf >/dev/null 2>&1; then
            dnf remove -y "$tool" >/dev/null
        elif command -v yum >/dev/null 2>&1; then
            yum remove -y "$tool" >/dev/null
        elif command -v microdnf >/dev/null 2>&1; then
            microdnf remove -y "$tool" >/dev/null
        elif command -v pacman >/dev/null 2>&1; then
            pacman -Rns --noconfirm "$tool" >/dev/null
        elif command -v zypper >/dev/null 2>&1; then
            zypper --non-interactive remove "$tool" >/dev/null
        fi
    fi
}

exit_error() {
  echo "$1"
  if [ "$SKIP_INSTALL" = false ]; then
    remove_tools curl
    remove_tools jq
  fi
  exit 1
}


if [ -n "${VAULT_SECRETS:-}" ] ; then

  if [ "$SKIP_INSTALL" = false ]; then
    echo "⬇️ Installation des outils..."
    add_tools curl
    add_tools jq
  fi

  echo "⏳ Vérification du status de Vault..."

  TIMEOUT=30
  COUNT=0
  READY=false

  while [ $COUNT -lt $TIMEOUT ] && [ "$READY" = false ] ; do
    sleep 2
    COUNT=$((COUNT+1))
    READY=true
    if ! curl -sf "$VAULT_ADDR/v1/sys/health" >/dev/null; then
      READY=false
    fi
  done

  if [ "$TIMEOUT" = "$COUNT" ] && [ "$READY" = false ]; then
    exit_error "⚠️ Vault non opérationnel ou contacte impossible, check si même réseau que vault !"
  else
    echo "⬇️ Chargement des variables..."
    set -a
    for e in $VAULT_SECRETS; do
      if [ "$IS_ROOT_SERVICE" = "true" ]; then
          policy="root"
          unset IS_ROOT_SERVICE
      else
          policy="app"
      fi

      # Authentification AppRole
      response=$(curl -sS \
          -w '\nHTTP_STATUS:%{http_code}' \
          -X POST \
          -H "Content-Type: application/json" \
          -d "$(jq -n \
              --arg role_id "$VAULT_ROLE_ID" \
              --arg secret_id "$VAULT_SECRET_ID" \
              '{role_id:$role_id,secret_id:$secret_id}')" \
          "$VAULT_ADDR/v1/auth/approle/login") \
          || exit_error "Erreur réseau vers Vault"

      http_status=$(printf '%s\n' "$response" |
          sed -n 's/^HTTP_STATUS://p')

      response_body=$(printf '%s\n' "$response" |
          sed '/^HTTP_STATUS:/d')

      if [ "$http_status" != "200" ]; then
          echo "Erreur AppRole HTTP $http_status"
          echo "$response_body"
          exit_error "Échec de l'authentification AppRole"
      fi

      VAULT_TOKEN=$(printf '%s' "$response_body" |
          jq -er '.auth.client_token') ||
          exit_error "Token absent de la réponse Vault"
      RESPONSE="$(curl -sS \
        --connect-timeout 5 \
        --max-time 15 \
        -H "X-Vault-Token: $VAULT_TOKEN" \
        "$VAULT_ADDR/v1/$policy/data/$e/config")"

      rc=$?

      if [ "$rc" -ne 0 ]; then
          unset RESPONSE
          exit_error "❌ Impossible de contacter Vault, veuillez mettre à jour le VAULT_TOKEN ! (curl rc=$rc) ${CURL_ERROR}"
      fi

      if ! OUTPUT=$(printf '%s' "$RESPONSE" | jq -er '
          .data.data
          | if type == "object" and length > 0 then
              to_entries[]
              | if (.key | test("^[a-zA-Z_][a-zA-Z0-9_]*$")) then
                  "export " + .key + "=" + (.value | tostring | @sh)
                else
                  error("Nom de variable invalide : " + .key)
                end
            else
              error("data.data absent ou vide")
            end
      '); then
          unset RESPONSE OUTPUT
          exit_error "❌ Erreur jq ou nom de variable invalide pour $e !"
      fi

      if ! eval "$OUTPUT"; then
          unset RESPONSE OUTPUT
          exit_error "❌ Erreur de chargement pour les variables de $e !"
      fi
      unset OUTPUT

      echo "✅ Variables de $e chargées"
      unset RESPONSE
    done
    set +a

    if [ "$SKIP_INSTALL" = false ]; then
        echo "🧹 Suppression des outils plus nécéssaire..."
        remove_tools curl
        remove_tools jq
    fi
  fi
else
  exit_error "⚠️ Service non configuré pour Vault !"
fi


echo "🚀 Lancement du service : $@"

if [ $# -eq 0 ]; then
  exit_error "❌ Aucune commande de démarrage fourni !"
fi
unset VAULT_ROLE_ID VAULT_SECRET_ID VAULT_SECRETS
exec "$@"