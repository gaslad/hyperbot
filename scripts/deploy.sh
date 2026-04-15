#!/bin/bash
# Deploy the Hyperbot landing page to Hostinger
# Usage: ./scripts/deploy.sh  (from any directory)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_DIR="${SCRIPT_DIR}/../website"

rsync -avz --delete \
  --exclude='.netlify' --exclude='.DS_Store' \
  -e "ssh -p 65002" \
  "$SITE_DIR/" \
  u951967435@82.197.83.159:domains/hyperbot.enseris.com/public_html/

ssh -p 65002 u951967435@82.197.83.159 \
  "find domains/hyperbot.enseris.com/public_html -type f -exec chmod 644 {} + && find domains/hyperbot.enseris.com/public_html -type d -exec chmod 755 {} +"

echo ""
echo "✓ Deployed to https://hyperbot.enseris.com"
