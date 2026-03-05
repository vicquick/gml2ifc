"""
WMS Elevation Fetcher
Fetch elevation raster data from WMS services and extract bounding boxes from shapefiles.
"""

import requests
import xml.etree.ElementTree as ET
import tempfile
import zipfile
from pathlib import Path


def get_wms_layers(wms_url):
    """
    Fetch WMS GetCapabilities and return list of available layers.
    Returns list of {name, title} dicts.
    """
    try:
        resp = requests.get(wms_url, params={
            'SERVICE': 'WMS',
            'REQUEST': 'GetCapabilities',
        }, timeout=30)
        resp.raise_for_status()

        root = ET.fromstring(resp.content)

        # Handle WMS namespaces (1.1.1 and 1.3.0)
        ns = ''
        if root.tag.startswith('{'):
            ns = root.tag.split('}')[0] + '}'

        layers = []
        for layer_elem in root.iter(f'{ns}Layer'):
            name_elem = layer_elem.find(f'{ns}Name')
            title_elem = layer_elem.find(f'{ns}Title')
            if name_elem is not None and name_elem.text:
                layers.append({
                    'name': name_elem.text.strip(),
                    'title': (title_elem.text.strip() if title_elem is not None and title_elem.text else name_elem.text.strip())
                })

        return {'success': True, 'layers': layers}

    except requests.exceptions.RequestException as e:
        return {'success': False, 'error': f'Request failed: {e}'}
    except ET.ParseError as e:
        return {'success': False, 'error': f'Failed to parse WMS capabilities XML: {e}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def extract_shapefile_bbox(zip_bytes):
    """
    Read a shapefile from ZIP bytes and return bounding box + EPSG code.
    Returns {success, bbox: (minx, miny, maxx, maxy), epsg_code} or error.
    """
    try:
        import geopandas as gpd

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "boundary.zip"
            zip_path.write_bytes(zip_bytes)

            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmpdir)

            shp_files = list(Path(tmpdir).glob("**/*.shp"))
            if not shp_files:
                return {'success': False, 'error': 'No .shp file found in the uploaded ZIP'}

            gdf = gpd.read_file(shp_files[0])

            if gdf.empty:
                return {'success': False, 'error': 'Shapefile contains no features'}

            bounds = gdf.total_bounds  # (minx, miny, maxx, maxy)
            bbox = (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))

            epsg_code = None
            if gdf.crs is not None:
                try:
                    epsg_code = gdf.crs.to_epsg()
                except Exception:
                    pass

            return {
                'success': True,
                'bbox': bbox,
                'epsg_code': epsg_code,
                'num_features': len(gdf),
            }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def fetch_wms_elevation_tif(wms_url, layer_name, bbox, crs_epsg):
    """
    Fetch a WMS GetMap request as GeoTIFF.
    bbox: (minx, miny, maxx, maxy)
    crs_epsg: integer EPSG code
    Returns {success, tif_bytes} or error.
    """
    try:
        minx, miny, maxx, maxy = bbox
        dx = maxx - minx
        dy = maxy - miny

        if dx <= 0 or dy <= 0:
            return {'success': False, 'error': 'Invalid bounding box dimensions'}

        # Calculate pixel dimensions (max 2048 on longest side)
        max_dim = 2048
        if dx >= dy:
            width = max_dim
            height = max(1, int(round(max_dim * dy / dx)))
        else:
            height = max_dim
            width = max(1, int(round(max_dim * dx / dy)))

        # WMS 1.3.0: EPSG:4326 uses lat/lon axis order (swap)
        if crs_epsg == 4326:
            bbox_str = f'{miny},{minx},{maxy},{maxx}'
        else:
            bbox_str = f'{minx},{miny},{maxx},{maxy}'

        params = {
            'SERVICE': 'WMS',
            'VERSION': '1.3.0',
            'REQUEST': 'GetMap',
            'LAYERS': layer_name,
            'CRS': f'EPSG:{crs_epsg}',
            'BBOX': bbox_str,
            'WIDTH': str(width),
            'HEIGHT': str(height),
            'FORMAT': 'image/tiff',
            'STYLES': '',
        }

        resp = requests.get(wms_url, params=params, timeout=120)
        resp.raise_for_status()

        content_type = resp.headers.get('Content-Type', '')

        # Check for XML error response
        if 'xml' in content_type.lower() or 'html' in content_type.lower():
            try:
                error_text = resp.text[:500]
                return {'success': False, 'error': f'WMS returned error: {error_text}'}
            except Exception:
                return {'success': False, 'error': 'WMS returned a non-image response'}

        if len(resp.content) < 100:
            return {'success': False, 'error': 'WMS returned an empty or too-small response'}

        return {
            'success': True,
            'tif_bytes': resp.content,
            'width': width,
            'height': height,
        }

    except requests.exceptions.RequestException as e:
        return {'success': False, 'error': f'Request failed: {e}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}
