#!/usr/bin/env bash
# install-sbctl.sh — symlink scripts/sbctl into ~/.local/bin.
#
# Idempotent: re-running upgrades existing symlinks. Refuses to clobber
# a non-symlink target so a hand-written sbctl in PATH never gets eaten.
# Optionally also installs a `pdctl` alias to the same script.
#
# Usage:
#   ./scripts/install-sbctl.sh            # install sbctl only
#   ./scripts/install-sbctl.sh --with-pdctl   # also install pdctl alias
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
SBCTL_SRC="$PROJECT_ROOT/scripts/sbctl"

if [ ! -f "$SBCTL_SRC" ]; then
  echo "install-sbctl: $SBCTL_SRC not found" >&2
  exit 1
fi

chmod +x "$SBCTL_SRC"

WITH_PDCTL="no"
for arg in "$@"; do
  case "$arg" in
    --with-pdctl|--pdctl) WITH_PDCTL="yes" ;;
    -h|--help)
      cat <<'EOF'
install-sbctl.sh — link scripts/sbctl into ~/.local/bin

Usage:
  ./scripts/install-sbctl.sh            install sbctl only
  ./scripts/install-sbctl.sh --with-pdctl  also install pdctl alias

Targets ~/.local/bin (created if missing). Existing symlinks are
upgraded; a non-symlink at the target path is left alone with an
error message so we never overwrite a real binary.
EOF
      exit 0
      ;;
  esac
done

LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"

install_link() {
  local name="$1"
  local target="$LOCAL_BIN/$name"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "install-sbctl: $target exists and is not a symlink — refusing to overwrite" >&2
    return 1
  fi
  ln -sfn "$SBCTL_SRC" "$target"
  echo "linked $target -> $SBCTL_SRC"
}

install_link sbctl

if [ "$WITH_PDCTL" = "yes" ]; then
  install_link pdctl
fi

case ":$PATH:" in
  *":$LOCAL_BIN:"*) ;;
  *)
    cat <<EOF

note: $LOCAL_BIN is not in your PATH. Add this to ~/.bashrc or ~/.zshrc:

    export PATH="\$HOME/.local/bin:\$PATH"

Then open a new shell (or 'source' the rc file) and run: sbctl status
EOF
    ;;
esac
