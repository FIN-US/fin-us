#!/usr/bin/env bash

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

cd "${ROOT}/mcp-news"
npm ci

cd "${ROOT}/mcp-trading"
npm ci

echo "OK: mcp-news and mcp-trading Node dependencies installed."
