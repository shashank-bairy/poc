// One place for every colour and label the UI uses, so the map legend and the
// panels can never drift apart.

export const SCHEME_COLORS = {
  geohash: '#e8833a',
  h3: '#1fa97d',
  s2: '#8d7fe0',
}

export const SCHEME_LABELS = {
  geohash: 'Geohash boxes (what Redis scans)',
  h3: 'H3 hexagons',
  s2: 'S2 squares',
}

export const POINT_COLORS = {
  base: '#5a5f6b',
  hit: '#00e5ff',
  rejected: '#ffb020',
}

export const METHOD_LABELS = {
  native: "The database's own geo index",
  h3: 'H3 hexagons on a plain lookup',
  s2: 'S2 ranges on a plain lookup',
}

export const METHOD_NAMES = {
  native: 'Native',
  h3: 'H3',
  s2: 'S2',
}

// How each store actually fetches the cells, shown under the lookup count.
export const ACCESS_PATH = {
  'postgres:h3': 'one query, = ANY(cells)',
  'postgres:s2': 'one query, joined on ranges',
  'redis:h3': 'one SUNION across the cell sets',
  'redis:s2': 'one pipelined batch of range scans',
  'aerospike:h3': 'one batch_read, cell ID is the key',
  'aerospike:s2': 'one query per range — not batchable',
}

export const STORE_LABELS = {
  postgres: 'Postgres / PostGIS',
  redis: 'Redis',
  aerospike: 'Aerospike',
}

// Short forms for the comparison table, where the column is narrow.
export const STORE_SHORT = {
  postgres: 'Postgres',
  redis: 'Redis',
  aerospike: 'Aerospike',
}
