import { useCallback, useEffect, useState } from 'react'

import { fetchCells, fetchMeta, fetchPoints, runQuery } from './api'
import ControlPanel from './components/ControlPanel'
import CompareTable from './components/CompareTable'
import MapView from './components/MapView'
import ResultsPanel from './components/ResultsPanel'

const DEFAULT_CENTER = { lat: 12.9716, lng: 77.5946 }

export default function App() {
  const [meta, setMeta] = useState(null)
  const [basePoints, setBasePoints] = useState([])
  const [center, setCenter] = useState(DEFAULT_CENTER)
  const [radius, setRadius] = useState(1000)
  const [store, setStore] = useState('postgres')
  const [method, setMethod] = useState('native')
  const [visibleSchemes, setVisibleSchemes] = useState({ geohash: false, h3: false, s2: false })
  const [cells, setCells] = useState({})
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetchMeta()
      .then((m) => {
        setMeta(m)
        setCenter(m.center)
        const first = m.stores.find((s) => s.available)
        if (first) setStore(first.name)
      })
      .catch((e) => setError(String(e.message ?? e)))
    fetchPoints(4000)
      .then((d) => setBasePoints(d.points))
      .catch(() => {})
  }, [])

  // Re-run the query whenever anything about it changes.
  useEffect(() => {
    if (!meta) return
    let stale = false
    setLoading(true)
    runQuery({ store, method, lat: center.lat, lng: center.lng, radius, repeat: 3 })
      .then((r) => {
        if (!stale) {
          setResult(r)
          setError(null)
        }
      })
      .catch((e) => !stale && setError(String(e.message ?? e)))
      .finally(() => !stale && setLoading(false))
    return () => {
      stale = true
    }
  }, [meta, store, method, center, radius])

  // Fetch cell outlines only for the schemes actually switched on.
  useEffect(() => {
    const wanted = Object.entries(visibleSchemes)
      .filter(([, on]) => on)
      .map(([s]) => s)
    if (!wanted.length) return
    let stale = false
    Promise.all(
      wanted.map((scheme) =>
        fetchCells({ scheme, lat: center.lat, lng: center.lng, radius }).then((d) => [scheme, d.cells]),
      ),
    )
      .then((pairs) => !stale && setCells(Object.fromEntries(pairs)))
      .catch(() => {})
    return () => {
      stale = true
    }
  }, [visibleSchemes, center, radius])

  // Switching store can strand you on a method that store does not have.
  useEffect(() => {
    if (!meta) return
    const available = meta.stores.find((s) => s.name === store)?.methods ?? []
    if (available.length && !available.includes(method)) setMethod('native')
  }, [meta, store, method])

  const toggleScheme = useCallback(
    (scheme) => setVisibleSchemes((v) => ({ ...v, [scheme]: !v[scheme] })),
    [],
  )

  return (
    <div className="app">
      <aside className="sidebar">
        <header>
          <h1>Where's the nearest thing?</h1>
          <p>
            {meta ? meta.point_count.toLocaleString() : '…'} points in three databases. Same
            question, six ways to answer it.
          </p>
        </header>

        <ControlPanel
          meta={meta}
          store={store}
          method={method}
          radius={radius}
          visibleSchemes={visibleSchemes}
          onStore={setStore}
          onMethod={setMethod}
          onRadius={setRadius}
          onToggleScheme={toggleScheme}
          onReset={() => setCenter(meta?.center ?? DEFAULT_CENTER)}
        />

        <ResultsPanel result={result} loading={loading} error={error} />
        <CompareTable center={center} radius={radius} />
      </aside>

      <main className="map-wrap">
        <MapView
          center={center}
          radius={radius}
          basePoints={basePoints}
          result={result}
          cells={cells}
          visibleSchemes={visibleSchemes}
          onPick={setCenter}
        />
      </main>
    </div>
  )
}
