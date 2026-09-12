#!/bin/sh
# Thin wrapper for macOS and Linux. All installer logic lives in install.py.
# Usage: ./setup.sh [install.py arguments]   e.g. ./setup.sh --target ../app --plan plus

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 &&
        "$py" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
        exec "$py" "$here/install.py" "$@"
    fi
done

printf '%s\n' 'Error: Python 3.8+ is required but was not found on PATH.' >&2
exit 1
