#!/bin/bash
# Deploy the Hyperbot landing page to Netlify
# Usage: ./scripts/deploy.sh  (from any directory)

set -e

SITE_ID="7a8e6d3d-4864-4c60-8928-6593a8e3429b"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_DIR="${SCRIPT_DIR}/../website"

if ! command -v netlify &> /dev/null; then
    echo "netlify-cli not found, using npx..."
    npx netlify-cli deploy --prod --dir "$SITE_DIR" --site "$SITE_ID"
else
    netlify deploy --prod --dir "$SITE_DIR" --site "$SITE_ID"
fi

echo ""
echo "✓ Deployed to https://hyperbot-landing.netlify.app"
