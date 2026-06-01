"""
DGM1 Tile Downloader
Parse DGM1 GeoJSON tile index, build selection grids, fetch XYZ files.
"""

import json
import io
import zipfile
import requests
from pyproj import Transformer


# ============================================================================
# PARSING
# ============================================================================

def parse_tile_index(geojson_bytes):
    """
    Parse DGM1 GeoJSON tile index.

    Expected feature properties:
      kachel:    "ZZXXXYYY"  (zone 2 + easting_km 3 + northing_km 4)
      datum:     "YYYY-MM-DD"
      link_data: download URL

    Returns:
        dict {kachel_str: {zone, x_km, y_km, datum, link_data}}
    """
    data = json.loads(geojson_bytes.decode('utf-8'))
    index = {}
    for feat in data.get('features', []):
        props = feat.get('properties', {})
        # Support both DGM1 ('kachel'/'link_data') and LoD2 ('id'/'data_link') schemas
        kachel = str(props.get('kachel') or props.get('id') or '').strip()
        link_data = props.get('link_data') or props.get('data_link') or ''
        if len(kachel) == 9 and kachel.isdigit():
            zone = int(kachel[:2])
            x_km = int(kachel[2:5])
            y_km = int(kachel[5:])
            index[kachel] = {
                'zone': zone,
                'x_km': x_km,
                'y_km': y_km,
                'datum': props.get('datum', ''),
                'link_data': link_data,
            }
    return index


# ============================================================================
# COORDINATE HELPERS
# ============================================================================

_transformers = {}

def _tr(epsg_from, epsg_to):
    key = (epsg_from, epsg_to)
    if key not in _transformers:
        _transformers[key] = Transformer.from_crs(
            f"EPSG:{epsg_from}", f"EPSG:{epsg_to}", always_xy=True
        )
    return _transformers[key]


def kachel_center_latlon(kachel):
    """Return (lat, lon) of tile center from kachel string."""
    zone = int(kachel[:2])
    x_km = int(kachel[2:5])
    y_km = int(kachel[5:])
    epsg = 25800 + zone
    lon, lat = _tr(epsg, 4326).transform(
        x_km * 1000 + 500,
        y_km * 1000 + 500,
    )
    return float(lat), float(lon)


def kachel_from_latlon(lat, lon, tile_index):
    """
    Map WGS84 click coordinates to a kachel ID.
    Tries UTM zones 32 and 33 (Germany coverage).
    Returns kachel string if found in tile_index, else None.
    """
    for zone in [32, 33]:
        epsg = 25800 + zone
        utm_x, utm_y = _tr(4326, epsg).transform(lon, lat)
        x_km = int(utm_x // 1000)
        y_km = int(utm_y // 1000)
        k = f"{zone}{x_km:03d}{y_km:04d}"
        if k in tile_index:
            return k
    return None


# ============================================================================
# GRID SELECTION
# ============================================================================

def parse_kachel(kachel):
    """Return (zone, x_km, y_km) from kachel string."""
    return int(kachel[:2]), int(kachel[2:5]), int(kachel[5:])


def get_grid_cells(center_kachel, tile_index, radius=1):
    """
    Build (2*radius+1) × (2*radius+1) grid around center_kachel.

    Returns list of cell dicts ordered N→S, W→E:
        {kachel, dx, dy, available, datum}
    """
    zone, cx, cy = parse_kachel(center_kachel)
    cells = []
    for dy in range(radius, -radius - 1, -1):     # N (positive dy) to S
        for dx in range(-radius, radius + 1):       # W to E
            x = cx + dx
            y = cy + dy
            k = f"{zone}{x:03d}{y:04d}"
            info = tile_index.get(k)
            cells.append({
                'kachel': k,
                'dx': dx,
                'dy': dy,
                'available': k in tile_index,
                'datum': info.get('datum', '') if info else '',
            })
    return cells


def grid_kachels(center_kachel, tile_index, radius=1):
    """Return set of available kachels in the grid."""
    return {c['kachel'] for c in get_grid_cells(center_kachel, tile_index, radius) if c['available']}


# ============================================================================
# MAP GEOJSON BUILDER
# ============================================================================

def build_view_geojson(tile_index, selected_kachels, center_kachel, radius=15):
    """
    Build WGS84 GeoJSON FeatureCollection for Folium.
    Only includes tiles within `radius` km of center tile.
    Tile boundaries are derived from x_km/y_km (exact 1 km squares).
    """
    zone, cx, cy = parse_kachel(center_kachel)
    epsg = 25800 + zone
    tr = _tr(epsg, 4326)

    features = []
    for kachel, info in tile_index.items():
        if info['zone'] != zone:
            continue
        if abs(info['x_km'] - cx) > radius or abs(info['y_km'] - cy) > radius:
            continue

        x0 = info['x_km'] * 1000
        y0 = info['y_km'] * 1000
        x1 = x0 + 1000
        y1 = y0 + 1000

        # Project tile corners SW→SE→NE→NW→SW
        ring = []
        for utm_x, utm_y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]:
            lon, lat = tr.transform(utm_x, utm_y)
            ring.append([float(lon), float(lat)])

        features.append({
            'type': 'Feature',
            'properties': {
                'kachel': kachel,
                'datum': info.get('datum', ''),
                'selected': kachel in selected_kachels,
                'is_center': kachel == center_kachel,
            },
            'geometry': {'type': 'Polygon', 'coordinates': [ring]},
        })

    return {'type': 'FeatureCollection', 'features': features}


# ============================================================================
# SERVER-SIDE DOWNLOAD
# ============================================================================

def download_tiles_zip(kachels, tile_index, progress_fn=None):
    """
    Download XYZ files for selected kachels from the tile index URLs.

    Args:
        kachels:      iterable of kachel strings to download
        tile_index:   dict from parse_tile_index()
        progress_fn:  optional callable(done, total, current_kachel)

    Returns:
        dict {success, zip_bytes, downloaded, failed}
          downloaded: [{kachel, filename, size_mb}]
          failed:     [{kachel, error}]
    """
    kachels = list(kachels)
    zip_buf = io.BytesIO()
    downloaded = []
    failed = []

    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, kachel in enumerate(kachels):
            if progress_fn:
                progress_fn(i, len(kachels), kachel)

            if kachel not in tile_index:
                failed.append({'kachel': kachel, 'error': 'Not in tile index'})
                continue

            url = tile_index[kachel].get('link_data', '')
            if not url:
                failed.append({'kachel': kachel, 'error': 'No download URL in index'})
                continue

            # Extract filename from URL query parameter
            if 'file=' in url:
                filename = url.split('file=')[1].split('&')[0]
            else:
                filename = f"dgm1_{kachel}.xyz"

            try:
                r = requests.get(url, timeout=180, stream=False)
                r.raise_for_status()
                zf.writestr(filename, r.content)
                downloaded.append({
                    'kachel': kachel,
                    'filename': filename,
                    'size_mb': len(r.content) / (1024 * 1024),
                })
            except Exception as e:
                failed.append({'kachel': kachel, 'error': str(e)})

    zip_buf.seek(0)
    return {
        'success': len(downloaded) > 0,
        'zip_bytes': zip_buf.getvalue(),
        'downloaded': downloaded,
        'failed': failed,
    }
