"""
Format Converter Module
XYZ point cloud → GeoTIFF (single file or batch ZIP)
XML (CityGML) → GML (extension rename with validation)
"""

import io
import zipfile
from pathlib import Path
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds


# ============================================================================
# XYZ → GEOTIFF
# ============================================================================

def analyze_xyz(xyz_bytes):
    """
    Parse XYZ bytes and return stats without building the raster.
    Used for metadata preview before conversion.
    """
    try:
        text = xyz_bytes.decode('utf-8', errors='replace')
        rows_data = []
        for line in text.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                try:
                    rows_data.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    continue

        if not rows_data:
            return {'success': False, 'error': 'No valid X Y Z rows found'}

        pts = np.array(rows_data, dtype=np.float64)
        xs, ys, zs = pts[:, 0], pts[:, 1], pts[:, 2]

        unique_x = np.unique(xs)
        unique_y = np.unique(ys)
        res_x = float(np.min(np.diff(unique_x))) if len(unique_x) > 1 else 1.0
        res_y = float(np.min(np.diff(unique_y))) if len(unique_y) > 1 else 1.0
        resolution = (res_x + res_y) / 2

        cols = round((xs.max() - xs.min()) / res_x) + 1
        rows_ = round((ys.max() - ys.min()) / res_y) + 1

        return {
            'success': True,
            'num_points': len(pts),
            'resolution': resolution,
            'res_x': res_x,
            'res_y': res_y,
            'width': cols,
            'height': rows_,
            'x_min': float(xs.min()),
            'x_max': float(xs.max()),
            'y_min': float(ys.min()),
            'y_max': float(ys.max()),
            'elevation_range': {
                'min': float(zs.min()),
                'max': float(zs.max()),
                'mean': float(zs.mean()),
            },
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def convert_xyz_to_tif(xyz_bytes, epsg_code=25832, nodata=-9999.0):
    """
    Convert XYZ point cloud (space-separated X Y Z) to GeoTIFF.

    Assumptions:
    - Regular grid (uniform spacing)
    - Points represent pixel centers
    - All points have valid elevation

    Args:
        xyz_bytes: Raw bytes of the .xyz file
        epsg_code: EPSG code for the output CRS (default 25832 = ETRS89 UTM 32N)
        nodata: NoData value for pixels with no point (default -9999.0)

    Returns:
        dict with success, tif_bytes, metadata
    """
    try:
        text = xyz_bytes.decode('utf-8', errors='replace')
        rows_data = []
        for line in text.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                try:
                    rows_data.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    continue

        if not rows_data:
            return {'success': False, 'error': 'No valid X Y Z rows found'}

        pts = np.array(rows_data, dtype=np.float64)
        xs, ys, zs = pts[:, 0], pts[:, 1], pts[:, 2]

        # Determine resolution from point spacing
        unique_x = np.sort(np.unique(xs))
        unique_y = np.sort(np.unique(ys))
        res_x = float(np.min(np.diff(unique_x))) if len(unique_x) > 1 else 1.0
        res_y = float(np.min(np.diff(unique_y))) if len(unique_y) > 1 else 1.0

        x_min, x_max = float(xs.min()), float(xs.max())
        y_min, y_max = float(ys.min()), float(ys.max())

        n_cols = round((x_max - x_min) / res_x) + 1
        n_rows = round((y_max - y_min) / res_y) + 1

        # Raster initialized to nodata
        raster = np.full((n_rows, n_cols), nodata, dtype=np.float32)

        # Vectorized pixel index assignment
        col_idx = np.round((xs - x_min) / res_x).astype(np.int32)
        # Y axis: raster row 0 = geographic top (y_max)
        row_idx = (n_rows - 1) - np.round((ys - y_min) / res_y).astype(np.int32)

        valid = (row_idx >= 0) & (row_idx < n_rows) & (col_idx >= 0) & (col_idx < n_cols)
        raster[row_idx[valid], col_idx[valid]] = zs[valid].astype(np.float32)

        # Affine transform: pixel corners enclose the point centers
        transform = from_bounds(
            x_min - res_x / 2,   # left
            y_min - res_y / 2,   # bottom
            x_max + res_x / 2,   # right
            y_max + res_y / 2,   # top
            n_cols,
            n_rows,
        )

        # Write GeoTIFF into memory
        with MemoryFile() as memfile:
            with memfile.open(
                driver='GTiff',
                height=n_rows,
                width=n_cols,
                count=1,
                dtype='float32',
                crs=f'EPSG:{epsg_code}',
                transform=transform,
                nodata=nodata,
                compress='lzw',
            ) as dst:
                dst.write(raster, 1)
            tif_bytes = memfile.read()

        return {
            'success': True,
            'tif_bytes': tif_bytes,
            'metadata': {
                'num_points': len(pts),
                'resolution': (res_x + res_y) / 2,
                'epsg_code': epsg_code,
                'width': n_cols,
                'height': n_rows,
                'elevation_range': {
                    'min': float(zs.min()),
                    'max': float(zs.max()),
                    'mean': float(zs.mean()),
                },
                'bounds': {
                    'left': x_min - res_x / 2,
                    'bottom': y_min - res_y / 2,
                    'right': x_max + res_x / 2,
                    'top': y_max + res_y / 2,
                },
            },
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


# ============================================================================
# XML → GML (CityGML pass-through)
# ============================================================================

def convert_xml_to_gml(xml_bytes):
    """
    Validate XML and return identical bytes for download as .gml.

    CityGML files are valid XML with GML namespaces. When delivered with .xml
    extension they need only a rename — content is unchanged.

    Args:
        xml_bytes: Raw bytes of the .xml file

    Returns:
        dict with success, gml_bytes, root_tag, encoding
    """
    try:
        import xml.etree.ElementTree as ET

        text = xml_bytes.decode('utf-8', errors='replace')

        # Validate parseable XML
        try:
            root = ET.fromstring(text.encode('utf-8'))
        except ET.ParseError as pe:
            return {'success': False, 'error': f'Invalid XML: {pe}'}

        # Extract root tag for info
        root_tag = root.tag
        # Strip namespace from tag for display
        if '{' in root_tag:
            ns, local = root_tag[1:].split('}', 1)
            display_tag = f"{local} (ns: {ns})"
        else:
            display_tag = root_tag

        # Check for GML/CityGML markers
        is_citygml = any(
            'citygml' in (root.attrib.get(k, '') or '').lower()
            or 'citygml' in k.lower()
            for k in root.attrib
        ) or 'citygml' in root_tag.lower()

        # Count top-level city object members
        members = [c for c in root if 'cityObjectMember' in c.tag]

        return {
            'success': True,
            'gml_bytes': xml_bytes,   # unchanged — exact source bytes
            'root_tag': display_tag,
            'is_citygml': is_citygml,
            'num_members': len(members),
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


# ============================================================================
# BATCH: XYZ ZIP → GEOTIFF ZIP
# ============================================================================

def convert_xyz_zip_to_tif_zip(zip_bytes, epsg_code=25832, nodata=-9999.0):
    """
    Convert a ZIP of .xyz files to a ZIP of GeoTIFFs.
    Each .xyz becomes a .tif with the same base name.
    """
    try:
        in_zip = zipfile.ZipFile(io.BytesIO(zip_bytes))
        xyz_names = sorted(n for n in in_zip.namelist() if n.lower().endswith('.xyz'))

        if not xyz_names:
            return {'success': False, 'error': 'No .xyz files found in ZIP'}

        out_buf = io.BytesIO()
        results = []

        with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as out_zip:
            for xyz_name in xyz_names:
                xyz_data = in_zip.read(xyz_name)
                tif_name = Path(xyz_name).stem + '.tif'
                result = convert_xyz_to_tif(xyz_data, epsg_code=epsg_code, nodata=nodata)
                if result['success']:
                    out_zip.writestr(tif_name, result['tif_bytes'])
                    results.append({
                        'input': xyz_name,
                        'output': tif_name,
                        'success': True,
                        'metadata': result['metadata'],
                    })
                else:
                    results.append({
                        'input': xyz_name,
                        'output': tif_name,
                        'success': False,
                        'error': result.get('error'),
                    })

        in_zip.close()
        out_buf.seek(0)
        success_count = sum(1 for r in results if r['success'])

        return {
            'success': success_count > 0,
            'zip_bytes': out_buf.getvalue(),
            'results': results,
            'num_success': success_count,
            'num_total': len(xyz_names),
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}
