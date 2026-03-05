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
                
                elevation_range = {
                    'min': float(np.min(valid_data)),
                    'max': float(np.max(valid_data)),
                    'mean': float(np.mean(valid_data))
                }
                
                return {
                    'success': True,
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