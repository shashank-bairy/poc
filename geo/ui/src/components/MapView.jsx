import { useEffect, useRef } from 'react'
import { MapContainer, TileLayer, useMap, useMapEvents } from 'react-leaflet'
import L from 'leaflet'

import { POINT_COLORS, SCHEME_COLORS } from '../constants'

// Point counts here run into the thousands. Creating that many React elements
// re-reconciles on every pan, so all heavy layers are drawn imperatively into a
// LayerGroup instead and React only manages the group's lifecycle.

function CanvasPoints({ points, color, radius, pane }) {
  const map = useMap()
  const groupRef = useRef(null)

  useEffect(() => {
    const group = L.layerGroup().addTo(map)
    groupRef.current = group
    return () => {
      group.remove()
    }
  }, [map])

  useEffect(() => {
    const group = groupRef.current
    if (!group) return
    group.clearLayers()
    for (const [, lat, lng] of points) {
      L.circleMarker([lat, lng], {
        radius,
        color,
        weight: 0,
        fillColor: color,
        fillOpacity: 0.85,
        pane,
        interactive: false,
      }).addTo(group)
    }
  }, [points, color, radius, pane])

  return null
}

function CellPolygons({ cells, color }) {
  const map = useMap()
  const groupRef = useRef(null)

  useEffect(() => {
    const group = L.layerGroup().addTo(map)
    groupRef.current = group
    return () => {
      group.remove()
    }
  }, [map])

  useEffect(() => {
    const group = groupRef.current
    if (!group) return
    group.clearLayers()
    for (const cell of cells) {
      L.polygon(cell.ring, {
        color,
        weight: 1.2,
        opacity: 0.9,
        fillColor: color,
        fillOpacity: 0.07,
      })
        .bindTooltip(cell.label, { sticky: true })
        .addTo(group)
    }
  }, [cells, color])

  return null
}

function QueryCircle({ center, radius }) {
  const map = useMap()

  useEffect(() => {
    const circle = L.circle([center.lat, center.lng], {
      radius,
      color: '#ffffff',
      weight: 2,
      dashArray: '6 5',
      fill: false,
      interactive: false,
    }).addTo(map)
    const marker = L.circleMarker([center.lat, center.lng], {
      radius: 5,
      color: '#ffffff',
      weight: 2,
      fillColor: '#111',
      fillOpacity: 1,
      interactive: false,
    }).addTo(map)
    return () => {
      circle.remove()
      marker.remove()
    }
  }, [map, center.lat, center.lng, radius])

  return null
}

function ClickHandler({ onPick }) {
  useMapEvents({
    click(e) {
      onPick({ lat: +e.latlng.lat.toFixed(6), lng: +e.latlng.lng.toFixed(6) })
    },
  })
  return null
}

function FitOnRadiusChange({ center, radius }) {
  const map = useMap()
  useEffect(() => {
    map.fitBounds(L.latLng(center.lat, center.lng).toBounds(radius * 4.5), {
      animate: true,
    })
    // Intentionally not reacting to `center`: re-fitting on every click would
    // fight the user's own panning.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [radius])
  return null
}

export default function MapView({
  center,
  radius,
  basePoints,
  result,
  cells,
  visibleSchemes,
  onPick,
}) {
  const hits = result?.hits?.map((h) => [h.id, h.lat, h.lng]) ?? []
  const rejected = result?.rejected ?? []

  return (
    <MapContainer
      center={[center.lat, center.lng]}
      zoom={13}
      preferCanvas
      className="map"
      worldCopyJump
    >
      {/* Plain OSM tiles (no API key). The dark look comes from a CSS filter
          on the tile pane -- see `.leaflet-tile-pane` in index.css. */}
      <TileLayer
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution="&copy; OpenStreetMap contributors"
        maxZoom={19}
      />

      <CanvasPoints points={basePoints} color={POINT_COLORS.base} radius={1.4} />
      <CanvasPoints points={rejected} color={POINT_COLORS.rejected} radius={2.6} />
      <CanvasPoints points={hits} color={POINT_COLORS.hit} radius={3.2} />

      {Object.entries(visibleSchemes)
        .filter(([, on]) => on)
        .map(([scheme]) => (
          <CellPolygons key={scheme} cells={cells[scheme] ?? []} color={SCHEME_COLORS[scheme]} />
        ))}

      <QueryCircle center={center} radius={radius} />
      <FitOnRadiusChange center={center} radius={radius} />
      <ClickHandler onPick={onPick} />
    </MapContainer>
  )
}
