#!/usr/bin/env bash
# Published compatibility package: runtime bytes are prepared by fork CI.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage=""
profile_home="${HERMES_HOME:-$HOME/.hermes}"
while [ $# -gt 0 ]; do
  case "$1" in
    --stage) stage="${2:?stage required}"; shift 2 ;;
    --hermes-home|-HermesHome) profile_home="${2:?home required}"; shift 2 ;;
    --dir|--branch) shift 2 ;;
    --skip-setup|--non-interactive) shift ;;
    *) printf 'Unsupported packaged installer argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
export HERMES_HOME="$profile_home"
verify_runtime() {
  "$root/.fork-payload/bin/hermes" --version
}
publish_command() {
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$root/.fork-payload/bin/hermes" "$HOME/.local/bin/hermes"
}
case "$stage" in
  venv|python-deps|node-deps) verify_runtime ;;
  path|products) publish_command ;;
  config) bash "$root/scripts/install-upstream.sh" --dir "$root" --hermes-home "$profile_home" --non-interactive --stage config ;;
  '') verify_runtime; publish_command
      bash "$root/scripts/install-upstream.sh" --dir "$root" --hermes-home "$profile_home" --non-interactive --stage config ;;
  *) printf 'Unsupported packaged installer stage: %s\n' "$stage" >&2; exit 2 ;;
esac
