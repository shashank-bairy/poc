import { ACCESS_PATH, POINT_COLORS } from '../constants'

function Stat({ label, value, sub, tone }) {
  return (
    <div className={`stat ${tone ?? ''}`}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  )
}

export default function ResultsPanel({ result, loading, error }) {
  if (error) return <section className="panel"><p className="error">{error}</p></section>
  if (!result) return <section className="panel"><p className="hint">Running…</p></section>

  const wrong = result.false_positives.length + result.false_negatives.length
  const overscan =
    result.candidates != null ? (result.candidates / Math.max(result.count, 1)).toFixed(1) : null

  return (
    <section className="panel">
      <h2>Result {loading && <span className="spinner" />}</h2>

      <div className="stats">
        <Stat label="Time taken" value={`${result.latency_ms} ms`} sub="median of 3 runs" />
        <Stat label="Points found" value={result.count} sub={`correct answer: ${result.truth_count}`} />
        <Stat
          label="Mistakes"
          value={wrong}
          sub={wrong ? `${result.false_positives.length} extra, ${result.false_negatives.length} missed` : 'exact match'}
          tone={wrong ? 'warn' : 'good'}
        />
        {result.candidates != null ? (
          <Stat
            label="Rows it had to read"
            value={result.candidates}
            sub={`${overscan}x more than it needed`}
            tone={overscan > 5 ? 'warn' : undefined}
          />
        ) : (
          <Stat label="Rows it had to read" value="—" sub="index filters internally" />
        )}
        {result.probes != null && (
          <Stat
            label={result.method === 'h3' ? 'Hexagons covered' : 'Ranges covered'}
            value={result.probes}
            sub={ACCESS_PATH[`${result.store}:${result.method}`]}
          />
        )}
      </div>

      <div className="legend">
        <span><i style={{ background: POINT_COLORS.hit }} /> inside the circle (the answer)</span>
        <span><i style={{ background: POINT_COLORS.rejected }} /> read from disk, then thrown away</span>
        <span><i style={{ background: POINT_COLORS.base }} /> everything else</span>
      </div>
    </section>
  )
}
