#!/bin/sh
set -e

echo "🚀 Generic full update (OS + common ecosystems)"

# Load OS info
if [ -f /etc/os-release ]; then
  . /etc/os-release
fi

#####################################
# 1. SYSTEM PACKAGES
#####################################

echo "📦 Updating OS packages..."

if command -v apk >/dev/null 2>&1; then
  echo "➡️ Alpine detected"
  apk update || true
  apk upgrade || true

elif command -v apt-get >/dev/null 2>&1; then
  echo "➡️ Debian/Ubuntu detected"
  apt-get update -y || true
  apt-get upgrade -y || true

elif command -v dnf >/dev/null 2>&1; then
  echo "➡️ RHEL/Fedora detected"
  dnf -y upgrade || true

elif command -v yum >/dev/null 2>&1; then
  echo "➡️ RHEL/YUM detected"
  yum -y update || true

else
  echo "⚠️ No package manager found - skipping OS update"
fi

#####################################
# 2. PYTHON
#####################################

if command -v python3 >/dev/null 2>&1; then
  echo "🐍 Python detected"

  PIP_FLAGS="--no-cache-dir --disable-pip-version-check --root-user-action=ignore"

  pip3 install $PIP_FLAGS pip-audit pip-tools || true

  find . \
    -path "*/venv" -prune -o \
    -path "*/.venv" -prune -o \
    -name "requirements.txt" -print | while IFS= read -r req; do

    [ -z "$req" ] && continue

    DIR=$(dirname "$req")

    echo ""
    echo "🟩 ===== Python project: $DIR ====="

    (
      cd "$DIR" || exit 1

      echo "📦 Installing locked dependencies"
      pip3 install $PIP_FLAGS -r requirements.txt || true

      echo "🔎 Checking dependency integrity"
      pip check || true

      echo "🔍 Running vulnerability scan"
      pip-audit -l || true

      echo "🛠️ Applying SAFE security fixes only"

      # Only fix vulnerabilities WITHOUT upgrading everything
      pip-audit -l --fix || true

      echo "📊 Final check"
      pip check || true
      pip-audit -l || true

    )

  done

else
  echo "⚠️ python3 not found"
fi

#####################################
# 3. NODE.JS
#####################################

if command -v npm >/dev/null 2>&1; then
  echo "🟢 Node.js detected"

  find . \
    -path "*/node_modules" -prune -o \
    -name "package.json" -print | while read -r pkg; do

    DIR=$(dirname "$pkg")
    echo ""
    echo "🟩 ===== Node project: $DIR ====="

    (
      cd "$DIR" || exit 1

      echo "📥 Installing dependencies (clean install)"
      npm install --no-audit --fund=false || true

      echo "🔎 Security audit (baseline)"
      npm audit || true

      echo "🛠️ Applying SAFE vulnerability fixes"

      # SAFE only: no breaking changes
      npm audit fix || true

      echo "🧹 Cleaning duplicates (safe)"
      npm dedupe || true

      echo "🔎 Post-fix audit check"
      npm audit || true

      echo "📊 Dependency summary"
      npm ls --depth=0 || true

    )
  done
else
  echo "⚠️ npm not found"
fi

#####################################

echo "✅ Full update completed (best effort mode)"
exit 0