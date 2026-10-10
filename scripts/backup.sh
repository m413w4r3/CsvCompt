#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
caller_dir="$PWD"

absolute_from_caller() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$caller_dir" "$1" ;;
  esac
}

resolved_args=()
while (($#)); do
  case "$1" in
    --output)
      if (($# >= 2)); then
        resolved_args+=(--output "$(absolute_from_caller "$2")")
        shift 2
      else
        resolved_args+=("$1")
        shift
      fi
      ;;
    --output=*)
      resolved_args+=("--output=$(absolute_from_caller "${1#--output=}")")
      shift
      ;;
    *)
      resolved_args+=("$1")
      shift
      ;;
  esac
done

cd "$project_root"
exec uv run python -m app.services.backups backup "${resolved_args[@]}"
