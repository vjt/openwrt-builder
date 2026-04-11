#!/bin/bash
set -euo pipefail

# Install the custom opkg feed and package on OpenWrt APs.
#
# Usage:
#   ./install-feed.sh                  # install on all APs from config
#   ./install-feed.sh golem pingu      # install on specific APs
#
# APs default to those listed in config.yaml (requires yq) or can be
# passed as arguments.

FEED_URL="http://opkg.bad.ass/all"
FEED_LINE="src/gz custom $FEED_URL"
FEED_CONF="/etc/opkg/customfeeds.conf"
PACKAGE="wifi-dethrash-collector"
SIGNING_PUB="runtime/feed-signing.pub"

if [ $# -eq 0 ]; then
    echo "Usage: $0 <ap1> [ap2 ...]" >&2
    echo "Example: $0 golem albert pingu gordon mowgli" >&2
    exit 1
fi

APS="$*"

if [ ! -f "$SIGNING_PUB" ]; then
    echo "Error: $SIGNING_PUB not found. Run the builder first to generate signing keys." >&2
    exit 1
fi

FP=$(docker compose exec builder usign -F -p /$SIGNING_PUB)

for ap in $APS; do
    echo "=== $ap ==="
    scp -O "$SIGNING_PUB" "$ap:/etc/opkg/keys/$FP"
    ssh "$ap" "
        grep -qF '$FEED_URL' $FEED_CONF 2>/dev/null || echo '$FEED_LINE' >> $FEED_CONF
        opkg update
        opkg install $PACKAGE
    "
    echo ""
done
