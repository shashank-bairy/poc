#!/usr/bin/env bash
set -euo pipefail
LOGDIR=/tmp/bolt-loadtest
PIDFILE="$LOGDIR/pids"

if [[ ! -f "$PIDFILE" ]]; then
	echo "no pidfile at $PIDFILE, nothing to stop"
	exit 0
fi

while read -r pid; do
	kill "$pid" 2>/dev/null || true
done < "$PIDFILE"
rm -f "$PIDFILE"
echo "stopped."
