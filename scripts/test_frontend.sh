#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)/frontend"
npm run test -- --run
