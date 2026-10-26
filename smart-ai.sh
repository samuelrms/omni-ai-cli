#!/usr/bin/env sh
# Compatibility shim: the router now lives in smart_ai.py (macOS, Linux and Windows).
here=$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)
exec python3 "$here/smart_ai.py" "$@"
