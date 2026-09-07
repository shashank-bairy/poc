#!/usr/bin/env bash
# Spawns app + a 100-stock producer + 5 clients (20 stocks each) for load testing.
# Logs go to /tmp/bolt-loadtest/. Ctrl+C or run scripts/stop-load-test.sh to tear down.
set -euo pipefail

cd "$(dirname "$0")/.."
LOGDIR=/tmp/bolt-loadtest
PIDFILE="$LOGDIR/pids"
mkdir -p "$LOGDIR"
: > "$PIDFILE"

STOCK_COUNT=${STOCK_COUNT:-100}
CLIENT_COUNT=${CLIENT_COUNT:-5}
STOCKS_PER_CLIENT=$(( STOCK_COUNT / CLIENT_COUNT ))

echo "building..."
go build -o "$LOGDIR/bolt-app" ./cmd
go build -o "$LOGDIR/producer" ./cmd/producer
go build -o "$LOGDIR/testclient" ./cmd/testclient

echo "starting app..."
"$LOGDIR/bolt-app" > "$LOGDIR/app.log" 2>&1 &
echo $! >> "$PIDFILE"
sleep 1

echo "starting producer ($STOCK_COUNT stocks)..."
STOCK_COUNT=$STOCK_COUNT "$LOGDIR/producer" > "$LOGDIR/producer.log" 2>&1 &
echo $! >> "$PIDFILE"
sleep 1

echo "starting $CLIENT_COUNT clients ($STOCKS_PER_CLIENT stocks each)..."
for i in $(seq 0 $((CLIENT_COUNT - 1))); do
	offset=$(( i * STOCKS_PER_CLIENT ))
	USER_ID="client$i" STOCK_COUNT=$STOCKS_PER_CLIENT STOCK_OFFSET=$offset \
		"$LOGDIR/testclient" > "$LOGDIR/client$i.log" 2>&1 &
	echo $! >> "$PIDFILE"
done

echo "all running. logs in $LOGDIR"
echo "tail -f $LOGDIR/client0.log   # etc."
echo "stop with: scripts/stop-load-test.sh"
