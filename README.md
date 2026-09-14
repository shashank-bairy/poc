# poc

Proof-of-concept projects.

## Projects

- [`bolt`](./bolt) — real-time stock tick broadcast over WebSockets. TCP ingest broker -> Redis pubsub -> WebSocket dispatcher fan-out. See [`bolt/README.md`](./bolt/README.md) for setup/run instructions and [`bolt/DISCUSSION_SUMMARY.md`](./bolt/DISCUSSION_SUMMARY.md) for HLD design notes.

- [`geo`](./geo) — geospatial indexing: Redis, Aerospike, Postgres/PostGIS and Elasticsearch native geo indexes compared against H3 and S2 cell schemes layered on all four. Includes a React + Leaflet UI for clicking around the map and watching each method answer. See [`geo/README.md`](./geo/README.md) for results and analysis.

## License

MIT — see [LICENSE](./LICENSE).
