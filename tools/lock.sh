#!/usr/bin/env bash
# tools/lock.sh — recompile the hash-pinned lock files from the readable pins.
#
#   ./tools/lock.sh
#
# requirements.txt (the engine) and tools/requirements.txt (the tools and
# pytest) are the sources; this writes requirements.lock and
# tools/requirements.lock with every package pinned and hashed, which is what
# the workflows install (pip install --require-hashes). To update a package:
# bump its pin in requirements.txt, run this, run the suite, one PR.
# Needs pip-tools in the active virtual environment (pip install pip-tools);
# it is a tool for this script, not a project dependency.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! python -m piptools --help >/dev/null 2>&1; then
  echo "pip-tools is not installed in this environment: pip install pip-tools" >&2
  exit 1
fi

compile() {
  python -m piptools compile --quiet --generate-hashes --strip-extras --no-emit-index-url \
    --allow-unsafe --rebuild "$@"
}

compile -o requirements.lock requirements.txt
compile -o tools/requirements.lock requirements.txt tools/requirements.txt

echo "requirements.lock: $(grep -c '^[a-zA-Z0-9]' requirements.lock) packages"
echo "tools/requirements.lock: $(grep -c '^[a-zA-Z0-9]' tools/requirements.lock) packages"
echo "Now run the suite (python -m pytest) and open one PR with the pin and both locks."
