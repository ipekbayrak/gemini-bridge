#!/bin/bash
set -euo pipefail

script_parent="."
case "$0" in
    */*) script_parent="${0%/*}" ;;
esac
SCRIPT_DIR="$(cd -- "${script_parent:-/}" && pwd)"
SELF_PATH="${SCRIPT_DIR}/${0##*/}"
BRIDGE_PY="${SCRIPT_DIR}/bridge.py"

if [ ! -f "${BRIDGE_PY}" ]; then
    echo "Error: bridge.py not found at ${BRIDGE_PY}" >&2
    exit 1
fi

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

is_py311_plus() {
    local bin="$1"
    [ -f "$bin" ] && [ -x "$bin" ] && [ ! -d "$bin" ] || return 1
    [ "$bin" != "$SELF_PATH" ] || return 1
    local real_bin
    if real_bin="$(resolve_full_path "$bin")"; then
        [ "$real_bin" != "$SELF_PATH" ] || return 1
    fi
    "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {
    local dir
    local name
    local candidate
    local sys_bins="${AGY_SETUP_SYSTEM_BINS-/opt/homebrew/bin:/usr/local/bin:/usr/bin}"
    local search_path="${PATH:-}"
    if [ -n "$sys_bins" ]; then
        if [ -n "$search_path" ]; then
            search_path="${search_path}:${sys_bins}"
        else
            search_path="${sys_bins}"
        fi
    fi

    local old_ifs="$IFS"
    set -f
    IFS=":"
    for dir in $search_path; do
        [ -n "$dir" ] || continue
        for name in python3 python3.14 python3.13 python3.12 python3.11 python; do
            candidate="${dir}/${name}"
            if is_py311_plus "$candidate"; then
                IFS="$old_ifs"
                set +f
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    done
    IFS="$old_ifs"
    set +f
    return 1
}

if ! PYTHON_BIN="$(find_python)"; then
    echo "Error: Python >= 3.11 is required to run antigravity-bridge, but was not found in PATH, /opt/homebrew/bin, or /usr/local/bin." >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    set -- serve
fi

exec "${PYTHON_BIN}" "${BRIDGE_PY}" "$@"
