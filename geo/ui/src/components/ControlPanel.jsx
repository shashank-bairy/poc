import { METHOD_LABELS, METHOD_NAMES, SCHEME_COLORS, SCHEME_LABELS, STORE_LABELS } from '../constants'

export default function ControlPanel({
  meta,
  store,
  method,
  radius,
  visibleSchemes,
  onStore,
  onMethod,
  onRadius,
  onToggleScheme,
  onReset,
}) {
  const radii = meta?.radii ?? [100, 1000, 10000]
  // Methods are per-store: only Aerospike exposes the batch-key path, because
  // only its layout has one record per H3 cell.
  const methods = meta?.stores?.find((s) => s.name === store)?.methods ?? ['native']

  return (
    <section className="panel">
      <h2>Query</h2>

      <label className="field">
        <span>Database</span>
        <div className="segmented">
          {(meta?.stores ?? []).map((s) => (
            <button
              key={s.name}
              className={store === s.name ? 'on' : ''}
              disabled={!s.available}
              title={s.error ?? ''}
              onClick={() => onStore(s.name)}
            >
              {STORE_LABELS[s.name] ?? s.name}
            </button>
          ))}
        </div>
      </label>

      <label className="field">
        <span>How it looks things up</span>
        <div className="stack">
          {methods.map((m) => (
            <button
              key={m}
              className={`row-btn ${method === m ? 'on' : ''}`}
              onClick={() => onMethod(m)}
            >
              <strong>{METHOD_NAMES[m] ?? m}</strong>
              <span>{METHOD_LABELS[m]}</span>
            </button>
          ))}
        </div>
      </label>

      <label className="field">
        <span>
          Search radius <em>{radius >= 1000 ? `${radius / 1000} km` : `${radius} m`}</em>
        </span>
        <input
          type="range"
          min={0}
          max={radii.length - 1}
          step={1}
          value={Math.max(0, radii.indexOf(radius))}
          onChange={(e) => onRadius(radii[+e.target.value])}
        />
        <div className="ticks">
          {radii.map((r) => (
            <span key={r}>{r >= 1000 ? `${r / 1000}k` : r}</span>
          ))}
        </div>
      </label>

      <h2>Show the grid</h2>
      <p className="hint">
        These are the boxes each scheme has to look inside. Bigger boxes than the circle means
        wasted work.
      </p>
      <div className="stack">
        {['geohash', 'h3', 's2'].map((scheme) => (
          <label key={scheme} className="check">
            <input
              type="checkbox"
              checked={visibleSchemes[scheme]}
              onChange={() => onToggleScheme(scheme)}
            />
            <i style={{ background: SCHEME_COLORS[scheme] }} />
            {SCHEME_LABELS[scheme]}
          </label>
        ))}
      </div>

      <button className="ghost" onClick={onReset}>
        Reset to city center
      </button>
      <p className="hint">Click anywhere on the map to move the search there.</p>
    </section>
  )
}
