"""
TIF to Shapefile Converter Module
Generates 3D contour polylines from elevation GeoTIFF files
"""

import io
import zipfile
from pathlib import Path
import numpy as np
import rasterio
from shapely.geometry import LineString
import geopandas as gpd
from pyproj import CRS as PyprojCRS
import tempfile
import os

# ============================================================================
# TIF METADATA EXTRACTION
# ============================================================================
def extract_tif_metadata(tif_bytes):
    """
    Extract metadata from GeoTIFF
    
    Returns:
        dict with keys: resolution, epsg_code, bounds, elevation_range, width, height
    """
    try:
        with rasterio.MemoryFile(tif_bytes) as memfile:
            with memfile.open() as src:
                # Get resolution (pixel size)
                resolution_x = abs(src.transform[0])
                resolution_y = abs(src.transform[4])
                resolution = (resolution_x + resolution_y) / 2  # Average
                
                # Get EPSG code
                epsg_code = None
                if src.crs:
                    epsg_code = src.crs.to_epsg()
                
                # Get bounds
                bounds = src.bounds
                
                # Read elevation data — convert to float64 to avoid NaN/uint8 conflict
                data = src.read(1)
                nodata = src.nodata
                data_float = data.astype(np.float64)
                if nodata is not None:
                    if np.isnan(nodata):
                        mask = np.isnan(data_float)
                    else:
                        mask = (data == nodata)
                    data_float[mask] = np.nan

                # Get elevation range (ignoring nodata/NaN)
                valid_data = data_float[~np.isnan(data_float)]
                total_pixels = data_float.size
                nodata_count = total_pixels - len(valid_data)
                nodata_pct = (nodata_count / total_pixels) * 100 if total_pixels > 0 else 0.0

                elevation_range = {
                    'min': float(np.min(valid_data)),
                    'max': float(np.max(valid_data)),
                    'mean': float(np.mean(valid_data))
                }

                return {
                    'success': True,
                    'nodata_pct': nodata_pct,
                    'resolution': resolution,
                    'resolution_x': resolution_x,
                    'resolution_y': resolution_y,
                    'epsg_code': epsg_code,
                    'bounds': {
                        'left': bounds.left,
                        'bottom': bounds.bottom,
                        'right': bounds.right,
                        'top': bounds.top
                    },
                    'elevation_range': elevation_range,
                    'width': src.width,
                    'height': src.height,
                    'nodata': src.nodata
                }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


# ============================================================================
# AUTO INTERVAL SUGGESTION
# ============================================================================
# Nice interval candidates (meters)
_NICE_INTERVALS = [0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0]

def suggest_contour_interval(resolution, elevation_min, elevation_max, target_contours=30):
    """
    Suggest a contour interval that produces a sensible number of contour lines.

    Picks from a list of "nice" intervals, targeting ~target_contours lines.
    Never suggests an interval smaller than the raster resolution.
    """
    span = elevation_max - elevation_min
    if span <= 0:
        return 1.0

    # Filter candidates: must be >= resolution (no point going finer than pixels)
    candidates = [iv for iv in _NICE_INTERVALS if iv >= resolution * 0.5]
    if not candidates:
        candidates = _NICE_INTERVALS  # fallback

    best = candidates[0]
    best_diff = abs(span / best - target_contours)
    for iv in candidates[1:]:
        n = span / iv
        diff = abs(n - target_contours)
        if diff < best_diff:
            best = iv
            best_diff = diff

    return best


# ============================================================================
# CONTOUR GENERATION
# ============================================================================
def generate_contours_from_tif(tif_bytes, interval=1.0, min_elevation=None, max_elevation=None):
    """
    Generate contour lines from GeoTIFF
    
    Args:
        tif_bytes: GeoTIFF file as bytes
        interval: Contour interval in elevation units
        min_elevation: Minimum elevation to generate contours (optional)
        max_elevation: Maximum elevation to generate contours (optional)
    
    Returns:
        dict with contours (list of dicts with geometry, elevation, epsg_code)
    """
    try:
        import matplotlib.pyplot as plt
        
        with rasterio.MemoryFile(tif_bytes) as memfile:
            with memfile.open() as src:
                # Read elevation data — convert to float64 to avoid NaN/uint8 conflict
                data = src.read(1)
                nodata = src.nodata
                data_float = data.astype(np.float64)
                if nodata is not None:
                    if np.isnan(nodata):
                        mask = np.isnan(data_float)
                    else:
                        mask = (data == nodata)
                    data_float[mask] = np.nan

                transform = src.transform
                epsg_code = src.crs.to_epsg() if src.crs else None

                # Determine elevation range
                valid_data = data_float[~np.isnan(data_float)]

                data_min = float(np.min(valid_data))
                data_max = float(np.max(valid_data))

                # Set contour levels
                if min_elevation is None:
                    min_elevation = data_min
                if max_elevation is None:
                    max_elevation = data_max

                # Generate contour levels
                levels = np.arange(
                    np.ceil(min_elevation / interval) * interval,
                    np.floor(max_elevation / interval) * interval + interval,
                    interval
                )

                if len(levels) == 0:
                    return {
                        'success': False,
                        'error': 'No contour levels generated with specified parameters'
                    }

                # Create coordinate arrays for matplotlib
                height, width = data_float.shape
                x = np.arange(width)
                y = np.arange(height)
                X, Y = np.meshgrid(x, y)
                
                # Create a figure without displaying it
                fig, ax = plt.subplots(figsize=(1, 1))
                
                # Generate contours using matplotlib
                try:
                    contour_set = ax.contour(X, Y, data_float, levels=levels)
                except Exception as e:
                    plt.close(fig)
                    return {
                        'success': False,
                        'error': f'Failed to generate contours: {str(e)}'
                    }
                
                # Extract contour line geometries
                contours = []
                
                # Iterate through each level
                for level_idx, level in enumerate(levels):
                    # Get paths for this level using allsegs attribute
                    # allsegs is a list (per level) of lists (per contour) of arrays (vertices)
                    if hasattr(contour_set, 'allsegs') and level_idx < len(contour_set.allsegs):
                        level_segs = contour_set.allsegs[level_idx]
                        
                        for segment in level_segs:
                            if len(segment) > 1:
                                # Transform pixel coordinates to geographic coordinates
                                # Add 0.5 to account for pixel center registration
                                geo_coords = []
                                for px, py in segment:
                                    # Use rasterio's transform * (col, row) method
                                    # Add 0.5 to convert from pixel corner to pixel center
                                    geo_x, geo_y = transform * (px + 0.5, py + 0.5)
                                    geo_coords.append((geo_x, geo_y))
                                
                                if len(geo_coords) > 1:
                                    line = LineString(geo_coords)
                                    if line.is_valid and line.length > 0:
                                        contours.append({
                                            'geometry': line,
                                            'elevation': float(level)
                                        })
                    
                    # Fallback: try using collections if allsegs doesn't exist
                    elif hasattr(contour_set, 'collections') and level_idx < len(contour_set.collections):
                        collection = contour_set.collections[level_idx]
                        paths = collection.get_paths()
                        
                        for path in paths:
                            vertices = path.vertices
                            
                            if len(vertices) > 1:
                                # Transform pixel coordinates to geographic coordinates
                                # Add 0.5 to account for pixel center registration
                                geo_coords = []
                                for px, py in vertices:
                                    # Use rasterio's transform * (col, row) method
                                    # Add 0.5 to convert from pixel corner to pixel center
                                    geo_x, geo_y = transform * (px + 0.5, py + 0.5)
                                    geo_coords.append((geo_x, geo_y))
                                
                                if len(geo_coords) > 1:
                                    line = LineString(geo_coords)
                                    if line.is_valid and line.length > 0:
                                        contours.append({
                                            'geometry': line,
                                            'elevation': float(level)
                                        })
                
                # Close the matplotlib figure
                plt.close(fig)
                
                if len(contours) == 0:
                    return {
                        'success': False,
                        'error': 'No contours generated - elevation data may not cross any contour levels'
                    }
                
                return {
                    'success': True,
                    'contours': contours,
                    'epsg_code': epsg_code,
                    'num_contours': len(contours),
                    'levels': levels.tolist()
                }
                
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


# ============================================================================
# SIMPLIFICATION ALGORITHMS
# ============================================================================
def simplify_douglas_peucker(geometry, tolerance):
    """
    Simplify geometry using Douglas-Peucker algorithm
    
    Args:
        geometry: Shapely LineString
        tolerance: Distance tolerance
    
    Returns:
        Simplified LineString
    """
    return geometry.simplify(tolerance, preserve_topology=True)


def simplify_chaikin(geometry, iterations=1):
    """
    Simplify geometry using Chaikin's corner cutting algorithm
    
    Args:
        geometry: Shapely LineString
        iterations: Number of smoothing iterations
    
    Returns:
        Smoothed LineString
    """
    coords = list(geometry.coords)
    
    for _ in range(iterations):
        if len(coords) < 2:
            break
        
        new_coords = []
        for i in range(len(coords) - 1):
            p1 = np.array(coords[i])
            p2 = np.array(coords[i + 1])
            
            # Create two new points at 1/4 and 3/4 along the line
            q = p1 + 0.25 * (p2 - p1)
            r = p1 + 0.75 * (p2 - p1)
            
            new_coords.append(tuple(q))
            new_coords.append(tuple(r))
        
        # Handle closed loops
        if coords[0] == coords[-1] and len(new_coords) > 0:
            new_coords.append(new_coords[0])
        
        coords = new_coords
    
    return LineString(coords) if len(coords) > 1 else geometry


def apply_simplification(contours, method='none', **params):
    """
    Apply simplification to contour geometries
    
    Args:
        contours: List of contour dicts with 'geometry' and 'elevation'
        method: 'none', 'douglas-peucker', or 'chaikin'
        **params: Method-specific parameters
    
    Returns:
        List of simplified contours
    """
    if method == 'none':
        return contours
    
    simplified = []
    for contour in contours:
        geom = contour['geometry']
        
        try:
            if method == 'douglas-peucker':
                tolerance = params.get('tolerance', 1.0)
                simplified_geom = simplify_douglas_peucker(geom, tolerance)
            elif method == 'chaikin':
                iterations = params.get('iterations', 1)
                simplified_geom = simplify_chaikin(geom, iterations)
            else:
                simplified_geom = geom
            
            simplified.append({
                'geometry': simplified_geom,
                'elevation': contour['elevation']
            })
        except Exception:
            # If simplification fails, keep original
            simplified.append(contour)
    
    return simplified


# ============================================================================
# SHAPEFILE EXPORT
# ============================================================================
def export_contours_to_shapefile_bytes(contours, epsg_code=None, output_crs=None, filename_base="contours"):
    """
    Export contours to shapefile as ZIP bytes, optionally reprojecting.

    Args:
        contours: List of contour dicts with 'geometry' and 'elevation'
        epsg_code: EPSG code of the source CRS
        output_crs: Target CRS for reprojection — can be an EPSG int, "EPSG:XXXX" string,
                     or a pyproj CRS object. None = keep source CRS.
        filename_base: Base name for shapefile

    Returns:
        dict with success, zip_bytes, num_features, output_crs_name
    """
    try:
        if not contours:
            return {
                'success': False,
                'error': 'No contours to export'
            }

        # Create GeoDataFrame with 3D geometries
        gdf_data = []
        for idx, contour in enumerate(contours):
            geom = contour['geometry']
            elevation = contour['elevation']

            # Add Z coordinate to create 3D polyline (PolyLineZ)
            coords_3d = [(x, y, elevation) for x, y in geom.coords]
            line_3d = LineString(coords_3d)

            gdf_data.append({
                'geometry': line_3d,
                'ELEVATION': elevation,
                'CONTOUR_ID': idx + 1
            })

        # Create GeoDataFrame
        gdf = gpd.GeoDataFrame(gdf_data)

        # Set CRS if provided
        if epsg_code:
            gdf.crs = f"EPSG:{epsg_code}"

        # Reproject if output CRS provided
        output_crs_name = str(epsg_code) if epsg_code else None
        if output_crs is not None and gdf.crs is not None:
            gdf = gdf.to_crs(output_crs)
            # Derive a display name for the output CRS
            try:
                out_epsg = gdf.crs.to_epsg()
                output_crs_name = str(out_epsg) if out_epsg else gdf.crs.name
            except Exception:
                output_crs_name = str(output_crs)

        # Create temporary directory for shapefile components
        with tempfile.TemporaryDirectory() as tmpdir:
            shp_path = os.path.join(tmpdir, f"{filename_base}.shp")

            # Write shapefile
            gdf.to_file(shp_path, driver='ESRI Shapefile')

            # Create ZIP file with all shapefile components
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                # Add all files with the same base name
                for file in os.listdir(tmpdir):
                    if file.startswith(filename_base):
                        file_path = os.path.join(tmpdir, file)
                        zip_file.write(file_path, file)

            zip_buffer.seek(0)

            return {
                'success': True,
                'zip_bytes': zip_buffer.getvalue(),
                'num_features': len(contours),
                'filename': f"{filename_base}.zip",
                'output_crs_name': output_crs_name,
            }

    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


# ============================================================================
# MAIN CONVERSION FUNCTION
# ============================================================================
def convert_tif_to_shapefile(
    tif_bytes,
    filename,
    interval=None,
    min_elevation=None,
    max_elevation=None,
    simplification_method='none',
    simplification_params=None,
    output_crs=None
):
    """
    Convert GeoTIFF to 3D contour shapefile

    Args:
        tif_bytes: GeoTIFF file as bytes
        filename: Original filename
        interval: Contour interval (auto-calculated if None)
        min_elevation: Minimum elevation filter
        max_elevation: Maximum elevation filter
        simplification_method: 'none', 'douglas-peucker', or 'chaikin'
        simplification_params: Dict of method-specific parameters
        output_crs: Target CRS (EPSG int, string, or pyproj CRS object; None = same as input)

    Returns:
        dict with success, zip_bytes, metadata, error
    """
    try:
        # Extract metadata
        metadata = extract_tif_metadata(tif_bytes)
        if not metadata['success']:
            return metadata

        # Auto-calculate interval if not provided
        if interval is None:
            interval = suggest_contour_interval(
                metadata['resolution'],
                metadata['elevation_range']['min'],
                metadata['elevation_range']['max'],
            )

        # Generate contours
        result = generate_contours_from_tif(
            tif_bytes,
            interval=interval,
            min_elevation=min_elevation,
            max_elevation=max_elevation
        )

        if not result['success']:
            return result

        contours = result['contours']

        if not contours:
            return {
                'success': False,
                'error': 'No contours generated'
            }

        # Apply simplification
        if simplification_params is None:
            simplification_params = {}

        contours = apply_simplification(
            contours,
            method=simplification_method,
            **simplification_params
        )

        # Export to shapefile
        filename_base = Path(filename).stem
        export_result = export_contours_to_shapefile_bytes(
            contours,
            epsg_code=result['epsg_code'],
            output_crs=output_crs,
            filename_base=filename_base
        )

        if not export_result['success']:
            return export_result

        return {
            'success': True,
            'zip_bytes': export_result['zip_bytes'],
            'filename': export_result['filename'],
            'metadata': {
                'epsg_code': result['epsg_code'],
                'output_crs_name': export_result.get('output_crs_name'),
                'num_contours': len(contours),
                'contour_levels': result['levels'],
                'interval': interval,
                'resolution': metadata['resolution'],
                'elevation_range': metadata['elevation_range'],
                'simplification': simplification_method
            }
        }

    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


# ============================================================================
# 3D ADAPTIVE POINT GRID GENERATION
# ============================================================================
def _prepare_raster(tif_bytes, clip_gdf=None):
    """
    Open raster, mask NoData, optionally compute pixel clip bounds.
    Returns (data, transform, epsg_code, row_start, row_end, col_start, col_end)
    or raises on failure.
    """
    memfile = rasterio.MemoryFile(tif_bytes)
    src = memfile.open()
    data = src.read(1).astype(np.float64)
    nodata = src.nodata
    if nodata is not None:
        if np.isnan(nodata):
            data[np.isnan(data)] = np.nan
        else:
            data[data == nodata] = np.nan
    data[data <= -9990] = np.nan

    transform = src.transform
    epsg_code = src.crs.to_epsg() if src.crs else None
    height, width = data.shape

    row_start, row_end = 0, height - 1
    col_start, col_end = 0, width - 1

    if clip_gdf is not None and not clip_gdf.empty:
        if clip_gdf.crs is not None and src.crs is not None:
            clip_gdf = clip_gdf.to_crs(src.crs)
        bounds = clip_gdf.total_bounds
        inv_transform = ~transform
        c_min, r_max = inv_transform * (bounds[0], bounds[1])
        c_max, r_min = inv_transform * (bounds[2], bounds[3])
        row_start = max(0, int(r_min))
        row_end = min(height - 1, int(r_max) + 1)
        col_start = max(0, int(c_min))
        col_end = min(width - 1, int(c_max) + 1)

    src.close()
    memfile.close()
    return data, transform, epsg_code, row_start, row_end, col_start, col_end


def generate_adaptive_points(tif_bytes, max_error=0.10, coarse_step=10,
                             clip_gdf=None):
    """
    Quadtree adaptive thinning: start coarse, subdivide cells where
    bilinear interpolation error exceeds threshold.

    Emits the CENTER point of each final (leaf) cell. Dense near slopes
    and NoData edges, sparse in flat areas.

    Args:
        tif_bytes: GeoTIFF file as bytes
        max_error: Maximum allowed elevation error in meters
        coarse_step: Starting grid step in pixels (subdivides down to 1)
        clip_gdf: Optional GeoDataFrame to clip extent

    Returns:
        dict with points list [{geometry, elevation}], epsg_code, stats
    """
    from shapely.geometry import Point

    try:
        data, transform, epsg_code, r0, r1, c0, c1 = _prepare_raster(tif_bytes, clip_gdf)
        height, width = data.shape

        points = []

        def _cell_error(r, c, step):
            """Max interpolation error within a cell defined by top-left (r,c) and size step."""
            r_end = min(r + step, height - 1)
            c_end = min(c + step, width - 1)
            if r_end <= r or c_end <= c:
                return 0.0, False

            z_tl = data[r, c]
            z_tr = data[r, c_end]
            z_bl = data[r_end, c]
            z_br = data[r_end, c_end]

            # If any corner is NaN, cell touches NoData — must refine
            if any(np.isnan(v) for v in [z_tl, z_tr, z_bl, z_br]):
                return float('inf'), True

            # Sample interior pixels
            cell = data[r:r_end + 1, c:c_end + 1]
            if cell.size <= 4:
                return 0.0, False

            rows_local = np.arange(cell.shape[0])
            cols_local = np.arange(cell.shape[1])
            fr = rows_local.astype(np.float64) / max(1, r_end - r)
            fc = cols_local.astype(np.float64) / max(1, c_end - c)
            FR, FC = np.meshgrid(fr, fc, indexing='ij')

            interp = (z_tl * (1 - FR) * (1 - FC) +
                      z_tr * (1 - FR) * FC +
                      z_bl * FR * (1 - FC) +
                      z_br * FR * FC)

            diff = np.abs(cell - interp)
            diff[np.isnan(cell)] = 0
            return float(np.nanmax(diff)), False

        def _process_cell(r, c, step):
            """Recursively process a cell: emit center or subdivide."""
            r_end = min(r + step, height - 1)
            c_end = min(c + step, width - 1)

            # Center pixel
            cr = (r + r_end) // 2
            cc = (c + c_end) // 2
            z_center = data[cr, cc] if cr < height and cc < width else np.nan

            if step <= 1 or (r_end - r <= 1 and c_end - c <= 1):
                # Leaf cell: emit center if valid
                if not np.isnan(z_center):
                    x, y = transform * (cc + 0.5, cr + 0.5)
                    points.append({
                        'geometry': Point(x, y, float(z_center)),
                        'elevation': float(z_center),
                    })
                return

            err, has_nodata = _cell_error(r, c, step)

            if err <= max_error and not has_nodata:
                # Cell is flat enough: emit just the center point
                if not np.isnan(z_center):
                    x, y = transform * (cc + 0.5, cr + 0.5)
                    points.append({
                        'geometry': Point(x, y, float(z_center)),
                        'elevation': float(z_center),
                    })
                return

            # Subdivide into 4 quadrants
            half = max(1, step // 2)
            r_mid = r + half
            c_mid = c + half

            _process_cell(r, c, half)
            if c_mid <= c1:
                _process_cell(r, c_mid, half)
            if r_mid <= r1:
                _process_cell(r_mid, c, half)
            if r_mid <= r1 and c_mid <= c1:
                _process_cell(r_mid, c_mid, half)

        # Increase recursion limit for deep quadtrees
        import sys
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, 10000))

        # Process all coarse cells
        for r in range(r0, r1 + 1, coarse_step):
            for c in range(c0, c1 + 1, coarse_step):
                _process_cell(r, c, coarse_step)

        sys.setrecursionlimit(old_limit)

        return {
            'success': True,
            'points': points,
            'epsg_code': epsg_code,
            'num_points': len(points),
            'max_error': max_error,
            'coarse_step': coarse_step,
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def generate_grid_points(tif_bytes, step=2, clip_gdf=None):
    """
    Generate a uniform grid of 3D loci from elevation GeoTIFF.

    Args:
        tif_bytes: GeoTIFF file as bytes
        step: Grid step in pixels
        clip_gdf: Optional GeoDataFrame to clip extent

    Returns:
        dict with points list, epsg_code, num_points, grid_step
    """
    from shapely.geometry import Point

    try:
        data, transform, epsg_code, r0, r1, c0, c1 = _prepare_raster(tif_bytes, clip_gdf)
        height, width = data.shape

        rows = np.arange(r0, r1 + 1, step)
        cols = np.arange(c0, c1 + 1, step)
        rows = rows[rows < height]
        cols = cols[cols < width]

        if len(rows) == 0 or len(cols) == 0:
            return {'success': False, 'error': 'Grid is empty after clipping'}

        # Vectorized extraction
        r_ix = rows[:, None]
        c_ix = cols[None, :]
        z_vals = data[r_ix, c_ix]
        valid = ~np.isnan(z_vals)

        # Geographic coordinates
        x_arr = transform[2] + (cols + 0.5) * transform[0]
        y_arr = transform[5] + (rows + 0.5) * transform[4]

        valid_r, valid_c = np.where(valid)
        points = []
        for i in range(len(valid_r)):
            ri, ci = valid_r[i], valid_c[i]
            z = float(z_vals[ri, ci])
            points.append({
                'geometry': Point(float(x_arr[ci]), float(y_arr[ri]), z),
                'elevation': z,
            })

        return {
            'success': True,
            'points': points,
            'epsg_code': epsg_code,
            'num_points': len(points),
            'grid_step': step,
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def export_points_to_shapefile_bytes(points, epsg_code=None, output_crs=None, filename_base="dtm_points"):
    """
    Export 3D points to PointZ shapefile as ZIP bytes, optionally reprojecting.
    """
    try:
        if not points:
            return {'success': False, 'error': 'No points to export'}

        gdf = gpd.GeoDataFrame(points)

        if epsg_code:
            gdf.crs = f"EPSG:{epsg_code}"

        output_crs_name = str(epsg_code) if epsg_code else None
        if output_crs is not None and gdf.crs is not None:
            gdf = gdf.to_crs(output_crs)
            try:
                out_epsg = gdf.crs.to_epsg()
                output_crs_name = str(out_epsg) if out_epsg else gdf.crs.name
            except Exception:
                output_crs_name = str(output_crs)

        with tempfile.TemporaryDirectory() as tmpdir:
            shp_path = os.path.join(tmpdir, f"{filename_base}.shp")
            gdf.to_file(shp_path, driver='ESRI Shapefile')

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for file in os.listdir(tmpdir):
                    if file.startswith(filename_base):
                        zip_file.write(os.path.join(tmpdir, file), file)

            zip_buffer.seek(0)
            return {
                'success': True,
                'zip_bytes': zip_buffer.getvalue(),
                'num_features': len(points),
                'filename': f"{filename_base}.zip",
                'output_crs_name': output_crs_name,
            }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def convert_tif_to_dtm_points(tif_bytes, filename, mode='adaptive',
                               step=2, max_error=0.10, coarse_step=10,
                               clip_gdf=None, output_crs=None):
    """
    Convert GeoTIFF to 3D point shapefile for VW DTM generation.

    Args:
        tif_bytes: GeoTIFF file as bytes
        filename: Original filename
        mode: 'adaptive' (smart thinning) or 'uniform' (fixed grid step)
        step: Grid step for uniform mode
        max_error: Max elevation error for adaptive mode (meters)
        coarse_step: Starting coarse step for adaptive mode
        clip_gdf: Optional GeoDataFrame for spatial clip
        output_crs: Target CRS for reprojection

    Returns:
        dict with success, zip_bytes, metadata
    """
    try:
        metadata = extract_tif_metadata(tif_bytes)
        if not metadata['success']:
            return metadata

        if mode == 'adaptive':
            result = generate_adaptive_points(
                tif_bytes, max_error=max_error, coarse_step=coarse_step,
                clip_gdf=clip_gdf,
            )
        else:
            result = generate_grid_points(tif_bytes, step=step, clip_gdf=clip_gdf)

        if not result['success']:
            return result

        filename_base = Path(filename).stem + "_dtm_points"
        export_result = export_points_to_shapefile_bytes(
            result['points'],
            epsg_code=result['epsg_code'],
            output_crs=output_crs,
            filename_base=filename_base,
        )

        if not export_result['success']:
            return export_result

        return {
            'success': True,
            'zip_bytes': export_result['zip_bytes'],
            'filename': export_result['filename'],
            'metadata': {
                'epsg_code': result['epsg_code'],
                'output_crs_name': export_result.get('output_crs_name'),
                'num_points': result['num_points'],
                'mode': mode,
                'max_error': max_error if mode == 'adaptive' else None,
                'coarse_step': coarse_step if mode == 'adaptive' else None,
                'grid_step': step if mode == 'uniform' else None,
                'resolution': metadata['resolution'],
                'elevation_range': metadata['elevation_range'],
                'nodata_pct': metadata.get('nodata_pct', 0),
            }
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}