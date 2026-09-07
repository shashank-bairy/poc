# HLD interview prep — real-time WS broadcast (bolt)

Notes from designing a real-time tick-broadcast system (TCP ingest → pub/sub bus → WebSocket fan-out to clients) at HLD interview depth.

## Baseline architecture

```
Ingest (broker) --publish--> pub/sub bus --subscribe--> Dispatcher (WS gateway) --> clients
```

- Ingest tier normalizes incoming data and publishes to a bus, keyed per symbol (e.g. `namespace:AAPL`).
- Dispatcher subscribes upstream **only for symbols it currently has a client for** (ref-counted: subscribe on first listener, unsubscribe when last listener leaves) — avoids paying fan-out cost for symbols nobody's watching.
- Client → dispatcher fan-out uses **non-blocking, drop-on-slow-client** delivery: a stalled/slow reader gets messages dropped rather than stalling delivery to everyone else. Correct default for a real-time feed — a dropped stale tick is worthless anyway, the next one supersedes it seconds later.

## Horizontal scaling — sharding strategy

**Shard by client, not by symbol.**

Why not by symbol: a client typically subscribes to multiple symbols over one WebSocket connection. If dispatcher instances each owned a subset of symbols, a client wanting AAPL + GOOG could be forced onto two different instances — meaning two sockets for one logical client. Breaks the single-connection-per-client model.

So: route by client — an LB places each client's connection on some dispatcher instance (round robin / least-connections, no special logic needed), and that instance independently subscribes upstream for whatever mix of symbols its own clients want.

Important precision point: this isn't "sharding" in the data-partitioning sense — nothing about symbol ownership is partitioned. Every dispatcher instance can subscribe to any symbol via the shared bus. It's **distributing connections**, not partitioning data. Worth stating explicitly if asked "so what's actually sharded here" — the honest answer is "nothing is partitioned, the bus's ref-counted subscribe model means the routing decision doesn't need to be."

Bonus property worth naming proactively: publish cost to the bus scales with *number of dispatcher instances currently having ≥1 client for that symbol*, not total client count — ref-counted subscribe means a hot symbol's tick fans out to at most N-instances at the bus layer, and each instance fans to its own clients locally. Good scaling property if asked about bus load.

## Connection limits per instance

~10K–65K connections/instance is a **default-`ulimit` ballpark** (Linux commonly defaults `ulimit -n` to 1024, tuned up to 65536 in prod configs) — not a hard architectural ceiling.

With epoll (Linux) or kqueue (macOS) — non-blocking, O(1)-per-event I/O — the polling mechanism itself scales to hundreds of thousands to low-millions of connections per box. The real ceiling becomes **memory** (each socket carries kernel TCP send/recv buffers, tens of KB by default × connection count) and tuned ulimits, not the I/O model. Real WS-heavy systems report low-millions of connections per box with tuned buffers/ulimits. Don't undersell the architecture's real ceiling by quoting the default-config number as if it were a hard limit.

## Load balancer routing

**Consistent hashing on client ID** for initial placement.

Not for data-locality (nothing's partitioned, per above) — the reason is **minimizing reconnect storms when the dispatcher fleet scales up/down**. With consistent hashing, adding/removing an instance remaps only ~1/N of the hash ring instead of every client needing to reconnect elsewhere.

Precision point: this only affects where *new or reconnecting* connections land. It doesn't migrate already-established live sockets — those stay put until they naturally disconnect.

## Capacity math — worked example

Chain: `messages/sec → bytes/sec (bandwidth) → instances needed`, then cross-check against a connection-count constraint and take the binding (larger) one.

**Assumptions:**
- 500 symbols, avg 10 ticks/sec/symbol
- avg 2,000 subscribers/symbol
- ~100 bytes/message (typical `{"type":"tick","payload":{...}}` JSON)

**Steps:**
```
1. Ingest rate:        500 symbols × 10 ticks/sec           = 5,000 ticks/sec
2. Fan-out deliveries:  5,000 × 2,000 subscribers/symbol     = 10,000,000 deliveries/sec
3. Bandwidth:           10,000,000 × 100 bytes               = ~1 GB/sec  (~8 Gbps)
4. Bandwidth-bound:     8,000 Mbps / 600 Mbps usable-per-box  ≈ 14 instances
5. Connection-bound:    1,000,000 clients / 100K per instance ≈ 10 instances
   → binding constraint = max(14, 10) = 14, provision ~14-16 with headroom
```

Key insight: fan-out (subscribers/symbol) is the multiplier that explodes the numbers — one tick becomes thousands of outbound deliveries. Always check **both** bandwidth and connection-count as independent constraints; candidates who check only one get caught by follow-ups.

## Reconnect / gap recovery

Explicitly **not needed** here — ticks are last-traded-price (LTP), inherently self-correcting: a missed tick during a brief disconnect is superseded by the next one. No sequence numbers, no replay/backfill required.

This is a good example of requirements shaping the design rather than defaulting to "add a durable log for safety" — the right answer is knowing *why* durability doesn't matter for this specific data shape, not reflexively reaching for Kafka.

## Multi-region

Client-facing WS tier stays **region-local** — client connects to nearest region via geo-DNS/anycast, never crosses regions on the message-delivery path.

Cross-region complexity is pushed to the **ingest/replication layer**: each region's ingest tier maintains a local copy of every symbol's stream, kept in sync via replication from the source (or from each other). Same principle as database read replicas — writes/ingest happen once, replicate to regional copies, reads (in this case, WS fan-out) stay local everywhere.

Cost still exists but moves: instead of "every tick, every client, crosses regions" (unbounded, scales with millions of clients), it's "every tick crosses regions once per region" (bounded by region count, ~3-5x). Bus systems with built-in interest-based cross-cluster propagation (see NATS below) can do this without hand-rolled replication code.

## Auth / authz

Two separate concerns:

- **Authentication (is this a real user?)** — once, at connection time. Short-lived JWT passed as a WS-upgrade query param (or first message), validated on upgrade; reject with 403 / close socket if invalid.
- **Authorization (can this user see this symbol?)** — per-subscribe-message, not just at connect. A lookup (cache-backed, e.g. `entitlements:{userId}` → allowed symbols/tier) inside the same handler that already validates subscribe requests. Not-entitled → reply with the same `error` frame used for other validation failures.

Worth noting: this is cheap to retrofit specifically *because* every subscribe already routes through one choke-point handler — one place to add the check, not a scattered retrofit.

## Observability

**System health (per instance):** connected-client count, per-symbol subscriber count (spot hot symbols), bus-connection health (silent failure mode — instance stops receiving ticks without crashing).

**Delivery quality (the real SLO):**
- Tick-to-client latency, P50/P99 — timestamp at ingest (source `ltt`), timestamp again right before the write to the client socket, diff. Most candidates only measure server-side processing time and miss the network leg by not timestamping at the true source.
- Dropped-message rate — every non-blocking-send drop, counted. High rate on one connection = bad client; system-wide spike = under-provisioned.
- Connection churn rate — spike usually signals an upstream problem (LB flapping, bad client-side deploy).

"How do you know it's working" is a standard real-time-system follow-up — the answer is naming these specific numbers, not "we'd add logging."

## Alternative transport: NATS instead of Redis pub/sub

Near 1:1 mapping for exact-symbol subscribe/unsubscribe (subject per symbol, e.g. `ticks.<namespace>.<stockId>`), plus optional hierarchical wildcard subscribe (`ticks.<namespace>.*`) that Redis pub/sub doesn't offer cleanly.

**Real advantage: multi-region.** NATS **superclusters** (clusters linked via gateways) do **interest-based propagation** — a publish in one region is only forwarded across the gateway to another region if that region actually has a live subscriber for the subject. This replaces hand-rolled broker-side replication from the multi-region answer above, and is strictly better than "always replicate everything everywhere" since it doesn't waste cross-region bandwidth on symbols nobody's watching there.

**Durability path:** core NATS pub/sub is fire-and-forget like Redis, matching LTP semantics. JetStream (NATS's persistence layer) is a drop-in upgrade if requirements ever needed replay/history — no re-architecture, just publish to a stream instead of a plain subject. Good to name as evidence the design has headroom without a rewrite.

**Trade-off against Redis:** Redis is likely already in the stack (caching/sessions) — reusing it for pub/sub is "no new infra." NATS is a dedicated new piece of infrastructure to operate. The win (superclustering, wildcards, durability upgrade path) has to justify that cost — reasonable answer: "start with Redis pub/sub for v1, NATS supercluster is the natural migration once multi-region interest-based routing becomes a real requirement, not before."

## Alternative design considered and rejected: KV-store interest map + direct TCP mesh

Proposal: replace the pub/sub bus with (a) a shared KV store (e.g. Aerospike) holding "which dispatcher instances are interested in which symbols," and (b) persistent TCP connections directly between every broker and every dispatcher, broker pushes ticks directly using the interest map.

This splits pub/sub into a control plane (interest registry) and data plane (direct push) — a real pattern in ultra-low-latency market-data feed handlers, but the specific mechanism has problems:

1. **Per-tick coupling to a database.** If the broker queries the KV store on every tick to know who to push to, that's a network round-trip to a DB at tick-rate frequency (thousands/sec) instead of subscription-change frequency (rare) — worse than pub/sub, where the routing decision is a local in-memory lookup inside the bus's own process.
2. **Caching the interest map locally just reintroduces pub/sub.** To avoid #1, cache the map in broker memory — but now something has to notify the broker when the map changes. That "something" is a notification/push mechanism, i.e. you've built pub/sub again, by hand, without the parts a real broker gives for free (subscription management, fan-out, backpressure).
3. **KV stores aren't notification systems.** Aerospike has no "tell me when this key changes, push to arbitrary subscribers" primitive — you either poll (adds lag) or build a push layer on top (back to #2).
4. **Mesh connection count is O(brokers × dispatchers)**, and every broker needs to discover every dispatcher instance to open a link — a service-discovery problem you now own, versus pub/sub where a new dispatcher instance just subscribes and the bus already knows about it.
5. **Failure detection is hand-built too** — dead TCP link to a crashed dispatcher needs heartbeat/keepalive detection and eviction from both the local cache and the KV store, duplicating logic a pub/sub broker already does automatically on subscriber disconnect.

**Where this pattern genuinely wins:** true HFT-tier microsecond latency, where the generic-broker hop is a real cost. But real systems doing this use **UDP multicast**, not per-connection unicast TCP (avoids O(N) broker-side writes per tick), with interest state kept in each feed handler's memory and refreshed via a lightweight control-plane channel — a KV store, if used at all, backs that control state durably rather than being queried on the hot path.

**Verdict to give if asked:** legitimate pattern, wrong default — only worth the operational complexity if sub-millisecond latency is a genuine, stated requirement; otherwise Redis/NATS pub/sub already solves routing freshness, fan-out, and failure detection for free, and the one-extra-hop cost is usually noise next to WAN/client-network latency.
