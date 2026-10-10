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
backup_resolved=0
while (($#)); do
  case "$1" in
    --target-db|--target-data-dir)
      option="$1"
      if (($# >= 2)); then
        resolved_args+=("$option" "$(absolute_from_caller "$2")")
        shift 2
      else
        resolved_args+=("$1")
        shift
      fi
      ;;
    --target-db=*|--target-data-dir=*)
      option="${1%%=*}"
      resolved_args+=("$option=$(absolute_from_caller "${1#*=}")")
      shift
      ;;
    --*)
      resolved_args+=("$1")
      shift
      ;;
    *)
      if [ "$backup_resolved" -eq 0 ]; then
        resolved_args+=("$(absolute_from_caller "$1")")
        backup_resolved=1
      else
        resolved_args+=("$1")
      fi
      shift
      ;;
  esac
done

cd "$project_root"
exec uv run python -m app.services.backups restore "${resolved_args[@]}"
