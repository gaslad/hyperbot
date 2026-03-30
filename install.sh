#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${HYPERBOT_REPO_URL:-https://github.com/gaslad/hyperbot.git}"
INSTALL_ROOT="${HYPERBOT_INSTALL_ROOT:-$HOME/Desktop/Hyperbot}"
BIN_DIR="${HYPERBOT_BIN_DIR:-$HOME/.local/bin}"
BRANCH="${HYPERBOT_BRANCH:-main}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

need_cmd git
need_cmd python3

mkdir -p "$BIN_DIR"

if [ -d "$INSTALL_ROOT/.git" ]; then
  echo "Updating existing Hyperbot install at $INSTALL_ROOT"
  git -C "$INSTALL_ROOT" fetch origin "$BRANCH" --depth=1
  git -C "$INSTALL_ROOT" checkout "$BRANCH"
  git -C "$INSTALL_ROOT" reset --hard "origin/$BRANCH"
else
  echo "Cloning Hyperbot into $INSTALL_ROOT"
  rm -rf "$INSTALL_ROOT"
  git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_ROOT"
fi

VENV_DIR="$INSTALL_ROOT/.venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment"
  python3 -m venv "$VENV_DIR"
fi

if [ -f "$INSTALL_ROOT/requirements.txt" ]; then
  echo "Installing Python dependencies"
  "$VENV_DIR/bin/pip" install -r "$INSTALL_ROOT/requirements.txt"
fi

cat > "$BIN_DIR/hyperbot" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$VENV_DIR/bin/python" "$INSTALL_ROOT/scripts/hyperbot.py" "\$@"
EOF

chmod +x "$BIN_DIR/hyperbot"

# Ensure hyperbot is on PATH for this session (and persist for future shells)
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    export PATH="$BIN_DIR:$PATH"
    # Add to shell profile if not already there
    SHELL_RC=""
    if [ -f "$HOME/.zshrc" ]; then
      SHELL_RC="$HOME/.zshrc"
    elif [ -f "$HOME/.bashrc" ]; then
      SHELL_RC="$HOME/.bashrc"
    elif [ -f "$HOME/.bash_profile" ]; then
      SHELL_RC="$HOME/.bash_profile"
    fi
    if [ -n "$SHELL_RC" ] && ! grep -q "$BIN_DIR" "$SHELL_RC" 2>/dev/null; then
      echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$SHELL_RC"
    fi
    ;;
esac

echo
echo "============================================"
echo "  Hyperbot installed successfully."
echo "============================================"
echo
echo "  Starts in test mode — no real trades until"
echo "  you explicitly enable live trading."
echo
echo "  Launching setup wizard..."
echo "============================================"
echo

# Launch the dashboard wizard directly
exec "$VENV_DIR/bin/python" "$INSTALL_ROOT/scripts/hyperbot.py" dashboard
