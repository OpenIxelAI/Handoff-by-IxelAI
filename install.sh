#!/usr/bin/env bash
# Installs Handoff into its own virtualenv and puts a `handoff` command on PATH.
#   From a checkout:  ./install.sh
#   Standalone:       bash install.sh   (clones HANDOFF_REPO_URL first)
set -euo pipefail

REPO_URL="${HANDOFF_REPO_URL:-https://github.com/OpenIxelAI/Handoff-by-IxelAI.git}"
BRANCH="${HANDOFF_BRANCH:-main}"
INSTALL_ROOT="${HANDOFF_INSTALL_ROOT:-$HOME/.local/share/handoff}"
BIN_DIR="${HANDOFF_BIN_DIR:-$HOME/.local/bin}"
REPO_DIR="$INSTALL_ROOT/repo"
VENV_DIR="$INSTALL_ROOT/.venv"
WRAPPER_PATH="$BIN_DIR/handoff"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: missing required command: $1" >&2
    os_hint "$1" >&2
    exit 1
  }
}

os_hint() {
  # One line telling the user how to get what's missing on this OS.
  local what="$1" id="" like=""
  if [[ "$(uname -s)" == "Darwin" ]]; then
    case "$what" in
      python) echo "  Install it with Homebrew:  brew install python@3.13   (https://brew.sh)" ;;
      git)    echo "  Install it with:  xcode-select --install   or   brew install git" ;;
    esac
    return
  fi
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    id="$(. /etc/os-release && echo "${ID:-}")"
    like="$(. /etc/os-release && echo "${ID_LIKE:-}")"
  fi
  case " $id $like " in
    *" debian "*|*" ubuntu "*)
      case "$what" in
        python) echo "  Install it with:  sudo apt install python3 python3-venv" ;;
        venv)   echo "  Install it with:  sudo apt install python3-venv   (or python3.X-venv for your version)" ;;
        git)    echo "  Install it with:  sudo apt install git" ;;
      esac ;;
    *" fedora "*|*" rhel "*|*" centos "*)
      case "$what" in
        python|venv) echo "  Install it with:  sudo dnf install python3" ;;
        git)         echo "  Install it with:  sudo dnf install git" ;;
      esac ;;
    *" arch "*)
      case "$what" in
        python|venv) echo "  Install it with:  sudo pacman -S python" ;;
        git)         echo "  Install it with:  sudo pacman -S git" ;;
      esac ;;
    *" suse "*|*" opensuse "*)
      case "$what" in
        python|venv) echo "  Install it with:  sudo zypper install python313   (or python311)" ;;
        git)         echo "  Install it with:  sudo zypper install git" ;;
      esac ;;
    *" alpine "*)
      case "$what" in
        python|venv) echo "  Install it with:  sudo apk add python3" ;;
        git)         echo "  Install it with:  sudo apk add git" ;;
      esac ;;
    *)
      echo "  Install $what with your system's package manager." ;;
  esac
}

python_ok() {
  # 3.10 or newer, and a final release: libraries Handoff needs can break on alphas and release candidates
  "$1" -c 'import sys; v = sys.version_info; sys.exit(0 if v >= (3, 10) and v.releaselevel == "final" else 1)' \
    >/dev/null 2>&1
}

pick_python() {
  # The system's python3 first when it's new enough (best supported by prebuilt
  # packages), then the newest versioned one, so an old default (macOS's 3.9)
  # doesn't stop us; Homebrew paths too, since GUI-launched shells often lack them.
  local candidate
  if [[ -n "${HANDOFF_PYTHON:-}" ]]; then
    if python_ok "$HANDOFF_PYTHON"; then
      command -v "$HANDOFF_PYTHON"
      return
    fi
    echo "error: HANDOFF_PYTHON=$HANDOFF_PYTHON isn't usable (too old, a pre-release, or it doesn't run)." >&2
    echo "  Handoff needs a final release of Python 3.10 or newer." >&2
    exit 1
  fi
  for candidate in python3 python python3.14 python3.13 python3.12 python3.11 python3.10 \
                   /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      command -v "$candidate"
      return
    fi
  done
  echo "error: Handoff needs Python 3.10 or newer (a final release, not an alpha or release candidate)." >&2
  if command -v python3 >/dev/null 2>&1; then
    candidate="$(python3 --version 2>/dev/null || true)"
    if [[ -n "$candidate" ]]; then
      echo "  Found $candidate at $(command -v python3), which isn't usable (too old, or a pre-release)." >&2
    fi
  fi
  os_hint python >&2
  echo "  Or set HANDOFF_PYTHON=/path/to/python3.10+ and run this again." >&2
  exit 1
}

venv_ok() {
  # The last install's environment can be kept: its Python is new enough, and it can install packages (with
  # uv, or its own pip; a run that stopped for a missing python3-venv can leave one without pip)
  [[ -e "$1" ]] && python_ok "$1" || return 1
  if [[ "${HANDOFF_USE_UV:-1}" != "0" ]] && command -v uv >/dev/null 2>&1; then
    return 0
  fi
  "$1" -m pip --version >/dev/null 2>&1
}

py_id() {
  # Which Python this is: its version and where it's installed (for a venv's python, its base's)
  "$1" -c 'import os, sys; print(sys.version, os.path.realpath(sys.base_prefix))' 2>/dev/null || true
}

append_path_hint() {
  if [[ "${HANDOFF_SKIP_PATH_UPDATE:-0}" == "1" ]]; then
    return
  fi
  case ":$PATH:" in
    *":$BIN_DIR:"*) return ;;
  esac

  local shell_name profile_line target_file=""
  shell_name="$(basename "${SHELL:-}")"
  profile_line="export PATH=\"$BIN_DIR:\$PATH\""

  case "$shell_name" in
    zsh) target_file="${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash)
      # macOS Terminal opens login shells, which read ~/.bash_profile, not ~/.bashrc
      if [[ "$(uname -s)" == "Darwin" ]]; then target_file="$HOME/.bash_profile"; else target_file="$HOME/.bashrc"; fi ;;
    fish)
      target_file="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/handoff.fish"
      profile_line="fish_add_path \"$BIN_DIR\"" ;;
  esac

  if [[ -n "$target_file" ]]; then
    mkdir -p "$(dirname "$target_file")"
    touch "$target_file"
    if ! grep -Fq "$profile_line" "$target_file"; then
      printf '\n# Added by Handoff installer\n%s\n' "$profile_line" >> "$target_file"
      echo "Added $BIN_DIR to PATH in $target_file"
    fi
  fi
}

VENV_PYTHON="$VENV_DIR/bin/python"
# An environment from the last install that works is kept, with its own Python (below), so updating it needs
# no other Python here. One is looked for only when there's no such environment, or you chose one.
if [[ -z "${HANDOFF_PYTHON:-}" ]] && venv_ok "$VENV_PYTHON"; then
  PYTHON_BIN="$VENV_PYTHON"
else
  PYTHON_BIN="$(pick_python)"
fi
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

# Install the checkout this script lives in, if it is one; otherwise clone.
SCRIPT_DIR=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/handoff" ]]; then
  SOURCE_DIR="$SCRIPT_DIR"
else
  need_cmd git
  if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" fetch origin
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"
  else
    rm -rf "$REPO_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
  fi
  SOURCE_DIR="$REPO_DIR"
fi

OLD_VENV=""
put_old_venv_back() {
  # A new environment that didn't work out (or was cut short) goes, and the last one comes back
  if [[ -n "$OLD_VENV" && -d "$OLD_VENV" ]]; then
    rm -rf "$VENV_DIR"
    mv "$OLD_VENV" "$VENV_DIR"
    echo "Put the last install's environment back, so Handoff works as it did before." >&2
  fi
}
trap put_old_venv_back EXIT
trap 'exit 130' INT TERM HUP
# An environment that works is kept, with the Python it has: an app may be running Handoff from it. It's made
# again only when you choose another Python with HANDOFF_PYTHON (handoff update passes the one you chose last
# time). The old one is moved aside, not deleted, until the new one works.
if venv_ok "$VENV_PYTHON" && \
   [[ -z "${HANDOFF_PYTHON:-}" || "$(py_id "$VENV_PYTHON")" == "$(py_id "$PYTHON_BIN")" ]]; then
  NEW_VENV=0
  echo "Using $("$VENV_PYTHON" --version 2>&1), in the environment from the last install"
else
  NEW_VENV=1
  if [[ -e "$VENV_DIR" ]]; then
    if venv_ok "$VENV_PYTHON"; then
      echo "Making a new environment with $PYTHON_BIN (the last install used another Python)"
    fi
    mv "$VENV_DIR" "$VENV_DIR.old-$$"
    OLD_VENV="$VENV_DIR.old-$$"
  fi
  echo "Using $("$PYTHON_BIN" --version 2>&1) at $PYTHON_BIN"
fi
if [[ "${HANDOFF_USE_UV:-1}" != "0" ]] && command -v uv >/dev/null 2>&1; then
  # uv is faster and doesn't need the distro's python3-venv package
  if [[ "$NEW_VENV" == 1 ]]; then
    uv venv --quiet --python "$PYTHON_BIN" "$VENV_DIR"
  fi
  uv pip install --quiet --python "$VENV_PYTHON" --upgrade "$SOURCE_DIR"
else
  if [[ "$NEW_VENV" == 1 ]] && ! "$PYTHON_BIN" -m venv "$VENV_DIR" >/dev/null 2>&1; then
    rm -rf "$VENV_DIR"  # what it made of one: kept, it would stop every run after this one
    echo "error: $PYTHON_BIN can't create virtual environments (the venv module is missing)." >&2
    os_hint venv >&2
    exit 1
  fi
  "$VENV_PYTHON" -m pip install --quiet --upgrade pip
  "$VENV_PYTHON" -m pip install --quiet --upgrade "$SOURCE_DIR"
fi
# Import everything the apps use, not just the entry point: a broken dependency
# should fail the install, not the first handoff.
if ! import_error="$("$VENV_PYTHON" -c "import handoff.cli, handoff.board, handoff.mcp_server" 2>&1)"; then
  echo "error: Handoff didn't finish installing: its libraries don't load on $("$VENV_PYTHON" --version 2>&1)." >&2
  printf '%s\n' "$import_error" | tail -n 3 | sed 's/^/  /' >&2
  echo "  Try another Python:  HANDOFF_PYTHON=/path/to/python3.12 bash \"$SOURCE_DIR/install.sh\"" >&2
  exit 1
fi
# The new environment works: the old one can go
if [[ -n "$OLD_VENV" ]]; then
  rm -rf "$OLD_VENV"
  OLD_VENV=""
fi

cat > "$WRAPPER_PATH" <<WRAPPER
#!/usr/bin/env bash
exec "$VENV_DIR/bin/handoff" "\$@"
WRAPPER
chmod +x "$WRAPPER_PATH"

append_path_hint

# Remember where this install came from, for `handoff update`, and the commit it installed. Last, so an
# install that fails keeps the last one's record, and `handoff update` knows to run this again. The Python
# you chose with HANDOFF_PYTHON is kept too, so `handoff update` uses it again (and when the environment
# was kept, so was its Python: the one recorded last time still holds).
COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)"
CHOSEN_PYTHON=""
if [[ -n "${HANDOFF_PYTHON:-}" ]]; then
  CHOSEN_PYTHON="$PYTHON_BIN"
elif [[ "$NEW_VENV" == 0 && -f "$INSTALL_ROOT/install.json" ]]; then
  CHOSEN_PYTHON="$("$VENV_PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("python") or "")' \
    "$INSTALL_ROOT/install.json" 2>/dev/null || true)"
fi
"$VENV_PYTHON" -c 'import json, sys; json.dump({"source": sys.argv[2], "install_root": sys.argv[3], "bin_dir": sys.argv[4], "installer": "install.sh", "commit": sys.argv[5], **({"python": sys.argv[6]} if sys.argv[6] else {})}, open(sys.argv[1], "w", encoding="utf-8"))' \
  "$INSTALL_ROOT/install.json" "$SOURCE_DIR" "$INSTALL_ROOT" "$BIN_DIR" "$COMMIT" "$CHOSEN_PYTHON"

echo
echo "Handoff installed from $SOURCE_DIR"
echo "Command: $WRAPPER_PATH"
echo "Next:    cd into a project, then run: handoff setup --write"
echo "Update:  handoff update"
echo "Remove:  handoff setup --remove, then: rm -rf \"$INSTALL_ROOT\" \"$WRAPPER_PATH\""
echo "If your shell cannot find 'handoff' yet, restart the shell or run:"
echo "  export PATH=\"$BIN_DIR:\$PATH\""
