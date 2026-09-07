# bolt

Tick broadcast POC: broker ingests ticks over TCP, publishes to Redis; dispatcher fans them out to WebSocket clients by stock subscription.

## Prereqs

- Go 1.27+
- Redis running locally (`redis-cli ping` → `PONG`)

## Run the app

```
go run ./cmd
```

- Broker listens on `:9000` (TCP, newline-delimited JSON ticks)
- Dispatcher listens on `:8000` (`/ws` WebSocket endpoint)

Env vars: `REDIS_ADDR` (default `localhost:6379`), `NAMESPACE` (default `bolt`).

## Manual testing

Send a tick to the broker:

```
echo '{"stockId":"AAPL","price":150,"ltt":1234567890}' | nc localhost 9000
```

Subscribe a client (see `cmd/testclient`), or use any WebSocket client to connect to `ws://localhost:8000/ws` and send:

```json
{"type":"subscribe","payload":{"userId":"u1","stockId":"AAPL"}}
```

## Dummy producer / client tools

`cmd/producer` — fakes tick generation, sends to broker over TCP.

```
go run ./cmd/producer
```

Env vars:
- `BROKER_ADDR` (default `localhost:9000`)
- `STOCKS` — comma list, e.g. `AAPL,GOOG` (default)
- `STOCK_COUNT` — if set, generates `STOCK1..STOCKN` instead of `STOCKS`

`cmd/testclient` — connects over WebSocket, subscribes, logs tick counts.

```
go run ./cmd/testclient
```

Env vars:
- `WS_URL` (default `ws://localhost:8000/ws`)
- `USER_ID` (default `u1`)
- `STOCK_IDS` — comma list, e.g. `AAPL,GOOG` (default `AAPL`)
- `STOCK_COUNT` + `STOCK_OFFSET` — if set, subscribes to `STOCK<offset+1>..STOCK<offset+N>` instead of `STOCK_IDS`

## Load test script

Spawns app + a 100-stock producer + 5 clients (20 stocks each) in the background.

```
./scripts/run-load-test.sh
```

Override scale:

```
STOCK_COUNT=200 CLIENT_COUNT=10 ./scripts/run-load-test.sh
```

Logs land in `/tmp/bolt-loadtest/` (`app.log`, `producer.log`, `client0.log` .. `clientN.log`).

```
tail -f /tmp/bolt-loadtest/client0.log
```

Stop everything:

```
./scripts/stop-load-test.sh
```
