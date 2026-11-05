#!/bin/bash
set -euo pipefail

script_parent="."
case "$0" in
    */*) script_parent="${0%/*}" ;;
esac
SCRIPT_DIR="$(cd -- "${script_parent:-/}" && pwd)"
SELF_PATH="${SCRIPT_DIR}/${0##*/}"

resolve_full_path() {
    local target="$1"
    local parent="."
    case "$target" in
        */*) parent="${target%/*}" ;;
    esac
    local resolved_dir
    resolved_dir="$(cd -- "${parent:-/}" 2>/dev/null && pwd)" || return 1
    printf '%s/%s\n' "${resolved_dir%/}" "${target##*/}"
}

is_valid_exec() {
    local bin="$1"
    [ -f "$bin" ] && [ -x "$bin" ] && [ ! -d "$bin" ] || return 1
    [ "$bin" != "$SELF_PATH" ] || return 1
    if [ -L "$bin" ]; then
        local link_target
        if link_target="$(PATH="${PATH:-}:/usr/bin:/bin" readlink "$bin" 2>/dev/null)"; then
            case "$link_target" in
                *launch-chrome.sh|*launch.sh) return 1 ;;
            esac
        fi
    fi
    local real_bin
    if real_bin="$(resolve_full_path "$bin")"; then
        [ "$real_bin" != "$SELF_PATH" ] || return 1
    fi
    return 0
}

collect_nvm_bins() {
    [ -n "${HOME:-}" ] && [ -d "${HOME}/.nvm/versions/node" ] || return 0
    local entry ver_dir ver major minor patch key
    local keys=()
    shopt -s nullglob
    for entry in "${HOME}/.nvm/versions/node"/v*/bin; do
        [ -d "$entry" ] || continue
        ver_dir="${entry%/bin}"
        ver="${ver_dir##*/}"
        ver="${ver#v}"
        if [[ "$ver" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(.*)$ ]]; then
            major="${BASH_REMATCH[1]}"
            minor="${BASH_REMATCH[2]}"
            patch="${BASH_REMATCH[3]}"
            printf -v key '%08d.%08d.%08d|%s' "$major" "$minor" "$patch" "$entry"
            keys+=("$key")
        fi
    done
    shopt -u nullglob

    local n="${#keys[@]}"
    local i j max_idx tmp
    for ((i = 0; i < n; i++)); do
        max_idx=$i
        for ((j = i + 1; j < n; j++)); do
            if [[ "${keys[j]}" > "${keys[max_idx]}" ]]; then
                max_idx=$j
            fi
        done
        if ((max_idx != i)); then
            tmp="${keys[i]}"
            keys[i]="${keys[max_idx]}"
            keys[max_idx]="$tmp"
        fi
        printf '%s\n' "${keys[i]#*|}"
    done
}

build_search_dirs() {
    local dir
    local old_ifs="$IFS"
    set -f
    IFS=":"
    for dir in ${PATH:-}; do
        [ -n "$dir" ] && printf '%s\n' "$dir"
    done
    IFS="$old_ifs"
    set +f

    if [ -n "${HOME:-}" ]; then
        [ -d "${HOME}/.local/bin" ] && printf '%s\n' "${HOME}/.local/bin"
        [ -d "${HOME}/.nvm/current/bin" ] && printf '%s\n' "${HOME}/.nvm/current/bin"
        [ -d "${HOME}/.volta/bin" ] && printf '%s\n' "${HOME}/.volta/bin"
        [ -d "${HOME}/.fnm/current/bin" ] && printf '%s\n' "${HOME}/.fnm/current/bin"
    fi

    collect_nvm_bins

    local sys_bins="${AGY_SETUP_SYSTEM_BINS-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin}"
    set -f
    IFS=":"
    for dir in $sys_bins; do
        [ -n "$dir" ] && printf '%s\n' "$dir"
    done
    IFS="$old_ifs"
    set +f
}

NODE_BIN=""
NPX_BIN=""

while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    if [ -z "$NODE_BIN" ] && is_valid_exec "${dir}/node"; then
        NODE_BIN="${dir}/node"
    fi
    if [ -z "$NPX_BIN" ] && is_valid_exec "${dir}/npx"; then
        NPX_BIN="${dir}/npx"
    fi
    if [ -n "$NODE_BIN" ] && [ -n "$NPX_BIN" ]; then
        break
    fi
done < <(build_search_dirs)

if [ -z "$NODE_BIN" ] || [ -z "$NPX_BIN" ]; then
    echo "Error: Node.js (node and npx) is required to launch chrome-devtools-mcp@1.12.0, but was not found in PATH, /opt/homebrew/bin, /usr/local/bin, or ~/.nvm/versions/node." >&2
    exit 1
fi

NODE_VERSION="$("${NODE_BIN}" --version 2>/dev/null)" || NODE_VERSION=""
NODE_SUPPORTED=false
if [[ "$NODE_VERSION" =~ ^v?([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    if (( major >= 23 || (major == 22 && minor >= 12) || (major == 20 && minor >= 19) )); then
        NODE_SUPPORTED=true
    fi
fi
if [ "$NODE_SUPPORTED" != true ]; then
    echo "Error: chrome-devtools-mcp@1.12.0 requires Node 20.19+, 22.12+, or 23+; found ${NODE_VERSION:-unknown}." >&2
    exit 1
fi

NODE_DIR="$(cd -- "${NODE_BIN%/*}" && pwd)"
NPX_DIR="$(cd -- "${NPX_BIN%/*}" && pwd)"

NEW_PATH="${NPX_DIR}"
if [ "${NODE_DIR}" != "${NPX_DIR}" ]; then
    NEW_PATH="${NODE_DIR}:${NEW_PATH}"
fi
if [ -n "${PATH:-}" ]; then
    NEW_PATH="${NEW_PATH}:${PATH}"
fi
export PATH="${NEW_PATH}"

exec "${NPX_BIN}" -y chrome-devtools-mcp@1.12.0 --isolated --headless --categoryExtensions --no-usage-statistics --no-performance-crux "$@"
