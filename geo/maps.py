"""Plot the three covering shapes over the same query disc.

Produces one folium HTML map per radius showing, as separate toggleable layers:
  - the true query circle
  - Redis' geohash cells (rectangles, axis-aligned, wildly over-covering)
  - H3's k-ring (hexagons, uniform size)
  - S2's covering (squares of mixed levels, hugging the circle)
  - the points themselves, split into "inside the radius" and "candidate only"

The point of the picture: all three schemes return a superset of the answer and
post-filter it. How much of a superset, and with how many index probes, is the
whole tradeoff.
"""

from __future__ import annotations

import argparse
import os

import folium
import h3
import s2sphere

import geohash_layer
import h3_layer
import s2_layer
from common import CENTER_LAT, CENTER_LNG, brute_force_radius, load_points

OUT_DIR = os.path.join(os.path.dirname(__file__), "maps")


def s2_cell_polygon(cell_id: s2sphere.CellId) -> list[tuple[float, float]]:
    cell = s2sphere.Cell(cell_id)
    ring = []
    for i in range(4):
        ll = s2sphere.LatLng.from_point(cell.get_vertex(i))
        ring.append((ll.lat().degrees, ll.lng().degrees))
    return ring


def build_map(lat: float, lng: float, radius_m: float, points, max_markers: int = 1500) -> folium.Map:
    zoom = {100: 16, 1000: 14, 10000: 11}.get(int(radius_m), 13)
    m = folium.Map(location=[lat, lng], zoom_start=zoom, tiles="OpenStreetMap")

    folium.Circle(
        [lat, lng],
        radius=radius_m,
        color="#111",
        weight=2,
        fill=False,
        tooltip=f"query radius {int(radius_m)} m",
    ).add_to(m)

    gh = folium.FeatureGroup(name="geohash cells (Redis)")
    for cell in geohash_layer.cells_for_disc(lat, lng, radius_m):
        lat_min, lat_max, lng_min, lng_max = geohash_layer.bbox(cell)
        folium.Rectangle(
            [(lat_min, lng_min), (lat_max, lng_max)],
            color="#d95f02",
            weight=1,
            fill=True,
            fill_opacity=0.05,
            tooltip=f"geohash {cell}",
        ).add_to(gh)
    gh.add_to(m)

    hx = folium.FeatureGroup(name="H3 k-ring (hexagons)")
    for cell in h3_layer.cells_for_disc(lat, lng, radius_m):
        folium.Polygon(
            [(la, ln) for la, ln in h3.cell_to_boundary(cell)],
            color="#1b9e77",
            weight=1,
            fill=True,
            fill_opacity=0.08,
            tooltip=f"h3 {cell}",
        ).add_to(hx)
    hx.add_to(m)

    s2g = folium.FeatureGroup(name="S2 covering (squares)")
    for cell_id in s2_layer.covering_cells(lat, lng, radius_m):
        folium.Polygon(
            s2_cell_polygon(cell_id),
            color="#7570b3",
            weight=1,
            fill=True,
            fill_opacity=0.08,
            tooltip=f"s2 level {cell_id.level()}",
        ).add_to(s2g)
    s2g.add_to(m)

    inside = {h.id for h in brute_force_radius(points, lat, lng, radius_m)}
    # Only plot points near the query, otherwise the map is unreadable.
    near = {h.id for h in brute_force_radius(points, lat, lng, radius_m * 2.5)}
    pts = folium.FeatureGroup(name="points")
    plotted = 0
    for p in points:
        if p.id not in near:
            continue
        # Cap the marker count -- a 10 km disc holds most of the dataset and the
        # resulting HTML runs to tens of megabytes otherwise.
        if p.id not in inside:
            plotted += 1
            if plotted > max_markers:
                continue
        folium.CircleMarker(
            [p.lat, p.lng],
            radius=2,
            color="#e7298a" if p.id in inside else "#999",
            fill=True,
            fill_opacity=0.8,
            tooltip=str(p.id),
        ).add_to(pts)
    pts.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--radii", default="100,1000,10000")
    ap.add_argument("--lat", type=float, default=CENTER_LAT)
    ap.add_argument("--lng", type=float, default=CENTER_LNG)
    ap.add_argument("--max-markers", type=int, default=1500, help="cap on out-of-radius markers drawn")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    points = load_points()
    for radius in (float(r) for r in args.radii.split(",")):
        m = build_map(args.lat, args.lng, radius, points, args.max_markers)
        path = os.path.join(OUT_DIR, f"coverage_{int(radius)}m.html")
        m.save(path)
        print(
            f"{int(radius):>6}m  geohash={len(geohash_layer.cells_for_disc(args.lat, args.lng, radius)):>3}"
            f"  h3={len(h3_layer.cells_for_disc(args.lat, args.lng, radius)):>4}"
            f"  s2={len(s2_layer.ranges_for_disc(args.lat, args.lng, radius)):>3}"
            f"  -> {path}"
        )


if __name__ == "__main__":
    main()
