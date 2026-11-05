#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export GOCACHE="${GOCACHE:-/tmp/openaria-gocache}"
go test ./... -count=1
go test -race ./... -count=1
go vet ./...
python3 scripts/update-integration.py
# Full installer generation: intentionally fails if DMG services or NSIS unavailable.
./scripts/build.sh
