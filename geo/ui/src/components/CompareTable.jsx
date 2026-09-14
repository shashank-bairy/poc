import { useState } from 'react'
import { runCompare } from '../api'
import { METHOD_LABELS, METHOD_NAMES, STORE_SHORT } from '../constants'

export default function CompareTable({ center, radius }) {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function run() {
    setBusy(true)
    setError(null)
    try {
      setData(await runCompare({ lat: center.lat, lng: center.lng, radius, repeat: 3 }))
    } catch (e) {
      setError(String(e.message ?? e))
    } finally {
      setBusy(false)
    }
  }

  const fastest = data ? Math.min(...data.rows.map((r) => r.latency_ms)) : null

  return (
    <section className="panel">
      <h2>Race them all</h2>
      <p className="hint">
        Runs this exact query on every database with every lookup method, and times each one.
      </p>
      <button className="primary" onClick={run} disabled={busy}>
        {busy ? 'Running…' : `Run at ${radius >= 1000 ? `${radius / 1000} km` : `${radius} m`}`}
      </button>

      {error && <p className="error">{error}</p>}

      {data && (
        <table className="cmp">
          <thead>
            <tr>
              <th>Database</th>
              <th>Method</th>
              <th>Time</th>
              <th>Read</th>
              <th>Lookups</th>
              <th>Wrong</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={`${r.store}-${r.method}`} className={r.latency_ms === fastest ? 'best' : ''}>
                <td>{STORE_SHORT[r.store] ?? r.store}</td>
                <td title={METHOD_LABELS[r.method]}>{METHOD_NAMES[r.method] ?? r.method}</td>
                <td className="num">{r.latency_ms} ms</td>
                <td className="num">{r.candidates ?? '—'}</td>
                <td className="num">{r.probes ?? '—'}</td>
                <td className="num">{r.false_positives + r.false_negatives}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {data && (
        <p className="hint">
          Correct answer is {data.truth_count} points. Fastest row highlighted.
        </p>
      )}
    </section>
  )
}
