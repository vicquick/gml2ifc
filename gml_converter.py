"""
GML to IFC Converter Module
Contains all conversion logic separated from UI
"""

from pathlib import Path
from xml.etree import ElementTree as ET
import ifcopenshell
import ifcopenshell.api
import re
import tempfile
import os
from pyproj import CRS, Transformer
from shapely.geometry import box as shapely_box

# ============================================================================
# GML NAMESPACES
# ============================================================================
NAMESPACES = {
    'gml': 'http://www.opengis.net/gml',
    'gml32': 'http://www.opengis.net/gml/3.2',
    'citygml': 'http://www.opengis.net/citygml/2.0',
    'citygml1': 'http://www.opengis.net/citygml/1.0',
    'bldg': 'http://www.opengis.net/citygml/building/2.0',
    'bldg1': 'http://www.opengis.net/citygml/building/1.0',
}

# All building namespace URIs to search
BLDG_NAMESPACES = [
    'http://www.opengis.net/citygml/building/2.0',
    'http://www.opengis.net/citygml/building/1.0',
]


# ============================================================================
# CRS OPTIONS
# ============================================================================
LS320_WKT = (
    'PROJCS["ETRS89 / Gauss-Kruger CM 9E (LS320)",'
    'GEOGCS["ETRS89",'
    'DATUM["European_Terrestrial_Reference_System_1989",'
    'SPHEROID["GRS 1980",6378137,298.257222101]],'
    'PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",0],'
    'PARAMETER["central_meridian",9],'
    'PARAMETER["scale_factor",1],'
    'PARAMETER["false_easting",3500000],'
    'PARAMETER["false_northing",0],'
    'UNIT["metre",1],'
    'AXIS["Easting",EAST],'
    'AXIS["Northing",NORTH]]'
)
LS320_CRS = CRS.from_wkt(LS320_WKT)

CRS_OPTIONS = {
    "EPSG:25832": "ETRS89 / UTM zone 32N",
    "EPSG:31467": "DHDN / 3-degree Gauss-Kruger zone 3 (3xxxxxx)",
    "EPSG:4647": "ETRS89 / UTM zone 32N (zE-N, 32xxxxxx)",
    "LS320": "Hamburg LS320 — ETRS89 / GK CM 9E (FE=3500000)",
}


def resolve_crs(key: str):
    """Return a pyproj-compatible CRS for a given key."""
    if key == "LS320":
        return LS320_CRS
    return key


# ============================================================================
# SURFACE TYPE COLORS — defaults from 3DCityDB conventions
# Keys map to CityGML bldg: surface type names; values are hex strings.
# ============================================================================
DEFAULT_SURFACE_COLORS = {
    'RoofSurface':          '#CC0000',  # red (classic CityGML roof)
    'WallSurface':          '#D9D1BA',  # warm light beige
    'GroundSurface':        '#808080',  # gray
    'OuterCeilingSurface':  '#B0B0B0',  # silver
    'OuterFloorSurface':    '#A0A0A0',  # dark silver
    'ClosureSurface':       '#C8C8C8',  # light gray
    'unknown':              '#D3D3D3',  # light gray fallback
}


def hex_to_rgb01(hex_str):
    """Convert '#RRGGBB' to (r, g, b) floats 0-1."""
    h = hex_str.lstrip('#')
    return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))


# ============================================================================
# EPSG EXTRACTION
# ============================================================================
def extract_epsg_from_gml(gml_content):
    """Extract EPSG code from GML content"""
    try:
        root = ET.fromstring(gml_content)
        
        # Try to find srsName attribute in various locations
        for ns_prefix in ['gml', 'gml32']:
            ns = NAMESPACES.get(ns_prefix)
            if ns:
                # Check in boundedBy/Envelope
                envelope = root.find(f'.//{{{ns}}}Envelope', NAMESPACES)
                if envelope is not None:
                    srs_name = envelope.get('srsName')
                    if srs_name:
                        epsg = extract_epsg_code(srs_name)
                        if epsg:
                            return epsg
                
                # Check in posList elements
                pos_list = root.find(f'.//{{{ns}}}posList', NAMESPACES)
                if pos_list is not None:
                    srs_name = pos_list.get('srsName')
                    if srs_name:
                        epsg = extract_epsg_code(srs_name)
                        if epsg:
                            return epsg
        
        return None
    except Exception:
        return None


def extract_epsg_code(srs_name):
    """Extract EPSG code from srsName string"""
    if not srs_name:
        return None
    
    # Try to find numbers after EPSG
    match = re.search(r'EPSG[:/]?:?(\d+)', srs_name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    
    return None


# ============================================================================
# GML PARSING
# ============================================================================
def parse_gml_polygons(gml_content):
    """Parse polygon surfaces from GML content - organized by surface"""
    try:
        root = ET.fromstring(gml_content)
        polygons = []
        
        # Look for Polygon elements in both GML namespaces
        for ns_prefix in ['gml', 'gml32']:
            ns = NAMESPACES.get(ns_prefix)
            if ns:
                for polygon in root.findall(f'.//{{{ns}}}Polygon', NAMESPACES):
                    polygon_coords = []
                    
                    # Get exterior ring
                    exterior = polygon.find(f'.//{{{ns}}}exterior', NAMESPACES)
                    if exterior is not None:
                        coords = parse_linear_ring(exterior, ns)
                        if coords:
                            polygon_coords.append(coords)
                    
                    # Get interior rings (holes)
                    for interior in polygon.findall(f'.//{{{ns}}}interior', NAMESPACES):
                        coords = parse_linear_ring(interior, ns)
                        if coords:
                            polygon_coords.append(coords)
                    
                    if polygon_coords:
                        polygons.append(polygon_coords)
        
        return polygons
    except Exception as e:
        raise Exception(f"Error parsing GML polygons: {e}")


def parse_linear_ring(ring_element, namespace):
    """Parse a LinearRing element and return coordinates"""
    linear_ring = ring_element.find(f'{{{namespace}}}LinearRing', NAMESPACES)
    if linear_ring is None:
        return None
    
    pos_list = linear_ring.find(f'{{{namespace}}}posList', NAMESPACES)
    if pos_list is None:
        return None
    
    text = pos_list.text.strip()
    values = [float(v) for v in text.split()]
    
    dim = int(pos_list.get('srsDimension', '3'))
    coords = []
    
    for i in range(0, len(values), dim):
        if dim == 2:
            coords.append((values[i], values[i+1], 0.0))
        else:
            coords.append((values[i], values[i+1], values[i+2]))
    
    return coords if coords else None


def _detect_surface_type(polygon_element, bldg_ns):
    """Walk up from a Polygon element to detect its CityGML surface type."""
    # Surface types we look for
    surface_types = ['RoofSurface', 'WallSurface', 'GroundSurface',
                     'OuterCeilingSurface', 'OuterFloorSurface', 'ClosureSurface']
    # Check the polygon's tag ancestry via the element's tag trail
    # Since ElementTree doesn't give parent refs, we detect by checking
    # if the polygon is nested inside a known surface container.
    # This is handled at parse time by the caller.
    return 'unknown'


def parse_buildings(gml_content):
    """Parse GML content into a list of buildings, each with its own polygons.

    Returns list of dicts: [{id, name, polygons, surface_types}, ...]
    polygons[i] corresponds to surface_types[i].
    Falls back to a single building if no <bldg:Building> elements found.
    """
    SURFACE_TYPE_NAMES = [
        'RoofSurface', 'WallSurface', 'GroundSurface',
        'OuterCeilingSurface', 'OuterFloorSurface', 'ClosureSurface',
    ]

    try:
        root = ET.fromstring(gml_content)
        buildings = []

        # Search across all known building namespaces (v1.0 and v2.0)
        building_elements = []
        for bldg_ns in BLDG_NAMESPACES:
            building_elements.extend(root.findall(f'.//{{{bldg_ns}}}Building'))

        if building_elements:
            for bldg_elem in building_elements:
                bldg_id = bldg_elem.get('{http://www.opengis.net/gml}id') or \
                          bldg_elem.get('{http://www.opengis.net/gml/3.2}id') or ''

                polygons = []
                surface_types = []

                # Build surface type tag map across all bldg namespaces
                surface_type_tags = {}
                for bldg_ns in BLDG_NAMESPACES:
                    for stype in SURFACE_TYPE_NAMES:
                        surface_type_tags[f'{{{bldg_ns}}}{stype}'] = stype

                # Parse polygons grouped by surface type containers
                for surface_tag, stype in surface_type_tags.items():
                    for surface_elem in bldg_elem.findall(f'.//{surface_tag}'):
                        for ns_prefix in ['gml', 'gml32']:
                            ns = NAMESPACES.get(ns_prefix)
                            if ns:
                                for polygon in surface_elem.findall(f'.//{{{ns}}}Polygon'):
                                    polygon_coords = _parse_polygon(polygon, ns)
                                    if polygon_coords:
                                        polygons.append(polygon_coords)
                                        surface_types.append(stype)

                # Grab any remaining polygons not inside a typed surface
                typed_polygon_ids = set()
                for surface_tag in surface_type_tags:
                    for surface_elem in bldg_elem.findall(f'.//{surface_tag}'):
                        for ns_prefix in ['gml', 'gml32']:
                            ns = NAMESPACES.get(ns_prefix)
                            if ns:
                                for p in surface_elem.findall(f'.//{{{ns}}}Polygon'):
                                    typed_polygon_ids.add(id(p))

                for ns_prefix in ['gml', 'gml32']:
                    ns = NAMESPACES.get(ns_prefix)
                    if ns:
                        for polygon in bldg_elem.findall(f'.//{{{ns}}}Polygon'):
                            if id(polygon) not in typed_polygon_ids:
                                polygon_coords = _parse_polygon(polygon, ns)
                                if polygon_coords:
                                    polygons.append(polygon_coords)
                                    surface_types.append('unknown')

                if polygons:
                    buildings.append({
                        'id': bldg_id,
                        'name': bldg_id or f'Building_{len(buildings)+1}',
                        'polygons': polygons,
                        'surface_types': surface_types,
                    })

        # Fallback: no bldg:Building found — treat whole file as one building
        if not buildings:
            polygons = parse_gml_polygons(gml_content)
            if polygons:
                buildings.append({
                    'id': '',
                    'name': 'GML Element',
                    'polygons': polygons,
                    'surface_types': ['unknown'] * len(polygons),
                })

        return buildings
    except Exception as e:
        raise Exception(f"Error parsing buildings: {e}")


def _parse_polygon(polygon_element, ns):
    """Parse a single GML Polygon element into coordinate rings."""
    polygon_coords = []
    exterior = polygon_element.find(f'.//{{{ns}}}exterior', NAMESPACES)
    if exterior is not None:
        coords = parse_linear_ring(exterior, ns)
        if coords:
            polygon_coords.append(coords)
    for interior in polygon_element.findall(f'.//{{{ns}}}interior', NAMESPACES):
        coords = parse_linear_ring(interior, ns)
        if coords:
            polygon_coords.append(coords)
    return polygon_coords if polygon_coords else None


def crop_buildings_by_boundary(buildings, boundary_polygon):
    """Filter buildings to only those intersecting the boundary polygon.

    Uses 2D bounding box of each building's coordinates for the intersection test.
    Returns (filtered_buildings, total_count, kept_count).
    """
    total = len(buildings)
    kept = []

    for bldg in buildings:
        # Collect all 2D coords from all polygons in this building
        all_x = []
        all_y = []
        for polygon_coords in bldg['polygons']:
            for ring in polygon_coords:
                for coord in ring:
                    all_x.append(coord[0])
                    all_y.append(coord[1])

        if not all_x:
            continue

        # Create 2D bounding box for this building
        bldg_bbox = shapely_box(min(all_x), min(all_y), max(all_x), max(all_y))

        if bldg_bbox.intersects(boundary_polygon):
            kept.append(bldg)

    return kept, total, len(kept)


def transform_building_coords(buildings, from_crs, to_crs):
    """Transform all building coordinates from one CRS to another.

    Transforms X,Y; keeps Z as-is. Modifies buildings in-place and returns them.
    """
    transformer = Transformer.from_crs(from_crs, to_crs, always_xy=True)

    for bldg in buildings:
        new_polygons = []
        for polygon_coords in bldg['polygons']:
            new_rings = []
            for ring in polygon_coords:
                new_coords = []
                for x, y, z in ring:
                    nx, ny = transformer.transform(x, y)
                    new_coords.append((nx, ny, z))
                new_rings.append(new_coords)
            new_rings_list = new_rings
            new_polygons.append(new_rings_list)
        bldg['polygons'] = new_polygons

    return buildings


def transform_polygon_coords(polygons, from_crs, to_crs):
    """Transform flat polygon list coordinates (legacy path). X,Y transformed; Z kept."""
    transformer = Transformer.from_crs(from_crs, to_crs, always_xy=True)

    new_polygons = []
    for polygon_coords in polygons:
        new_rings = []
        for ring in polygon_coords:
            new_coords = []
            for x, y, z in ring:
                nx, ny = transformer.transform(x, y)
                new_coords.append((nx, ny, z))
            new_rings.append(new_coords)
        new_polygons.append(new_rings)

    return new_polygons


# ============================================================================
# COORDINATE CALCULATIONS
# ============================================================================
def get_coordinate_bounds(polygons):
    """Calculate bounding box of all polygons"""
    if not polygons:
        return None
    
    min_x = min_y = min_z = float('inf')
    max_x = max_y = max_z = float('-inf')
    
    for polygon_coords in polygons:
        for ring in polygon_coords:
            for coord in ring:
                min_x = min(min_x, coord[0])
                max_x = max(max_x, coord[0])
                min_y = min(min_y, coord[1])
                max_y = max(max_y, coord[1])
                min_z = min(min_z, coord[2])
                max_z = max(max_z, coord[2])
    
    return {
        'min': (min_x, min_y, min_z),
        'max': (max_x, max_y, max_z),
        'center': ((min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2)
    }


# ============================================================================
# IFC CREATION
# ============================================================================
def create_ifc_file(epsg_code=None, use_map_conversion=False):
    """Create a new IFC4X3 file with proper setup"""
    # Create IFC file
    ifc_file = ifcopenshell.file(schema='IFC4X3')
    
    # Create project
    project = ifcopenshell.api.run(
        "root.create_entity",
        ifc_file,
        ifc_class="IfcProject",
        name="GML Conversion Project"
    )
    
    # Set up units (metric)
    ifcopenshell.api.run(
        "unit.assign_unit",
        ifc_file,
        length={"is_metric": True, "raw": "METERS"}
    )
    
    # Create geometric representation context
    context = ifcopenshell.api.run(
        "context.add_context",
        ifc_file,
        context_type="Model"
    )
    
    body = ifcopenshell.api.run(
        "context.add_context",
        ifc_file,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=context
    )
    
    # Create site
    site = ifcopenshell.api.run(
        "root.create_entity",
        ifc_file,
        ifc_class="IfcSite",
        name="Site"
    )
    
    # Set site placement at origin
    origin = ifc_file.createIfcCartesianPoint((0.0, 0.0, 0.0))
    axis = ifc_file.createIfcDirection((0.0, 0.0, 1.0))
    ref_direction = ifc_file.createIfcDirection((1.0, 0.0, 0.0))
    placement = ifc_file.createIfcAxis2Placement3D(origin, axis, ref_direction)
    site.ObjectPlacement = ifc_file.createIfcLocalPlacement(None, placement)
    
    # Link site to project
    ifcopenshell.api.run(
        "aggregate.assign_object",
        ifc_file,
        relating_object=project,
        products=[site]
    )
    
    # Add coordinate reference system if EPSG is provided
    if epsg_code and use_map_conversion:
        try:
            map_conversion = ifc_file.createIfcMapConversion(
                SourceCRS=context,
                TargetCRS=ifc_file.createIfcProjectedCRS(
                    Name=f"EPSG:{epsg_code}",
                    MapUnit=ifc_file.createIfcSIUnit(None, "LENGTHUNIT", None, "METRE"),
                    GeodeticDatum=f"EPSG:{epsg_code}",
                    VerticalDatum=None
                ),
                Eastings=0.0,
                Northings=0.0,
                OrthogonalHeight=0.0,
                XAxisAbscissa=1.0,
                XAxisOrdinate=0.0,
                Scale=1.0
            )
            
            if not context.HasCoordinateOperation:
                context.HasCoordinateOperation = (map_conversion,)
        except Exception:
            pass  # Silently fail if MapConversion not supported
    
    return ifc_file, site, body


def create_face_surface(ifc_file, context, polygon_coords):
    """Create an IfcFace from polygon coordinates (exterior + holes)"""
    try:
        # Outer boundary
        outer_coords = polygon_coords[0]
        outer_points = [ifc_file.createIfcCartesianPoint(coord) for coord in outer_coords]
        outer_loop = ifc_file.createIfcPolyLoop(outer_points)
        outer_bound = ifc_file.createIfcFaceOuterBound(outer_loop, True)
        
        # Inner boundaries (holes)
        inner_bounds = []
        if len(polygon_coords) > 1:
            for inner_coords in polygon_coords[1:]:
                inner_points = [ifc_file.createIfcCartesianPoint(coord) for coord in inner_coords]
                inner_loop = ifc_file.createIfcPolyLoop(inner_points)
                inner_bound = ifc_file.createIfcFaceBound(inner_loop, True)
                inner_bounds.append(inner_bound)
        
        # Create face
        bounds = [outer_bound] + inner_bounds
        face = ifc_file.createIfcFace(bounds)
        
        return face
    except Exception:
        return None


def _get_or_create_surface_style(ifc_file, style_cache, surface_type, color_map):
    """Get or create an IfcSurfaceStyle for a surface type, with caching."""
    if surface_type in style_cache:
        return style_cache[surface_type]

    hex_color = color_map.get(surface_type, color_map.get('unknown', '#D3D3D3'))
    r, g, b = hex_to_rgb01(hex_color)

    colour_rgb = ifc_file.createIfcColourRgb(None, r, g, b)
    rendering = ifc_file.createIfcSurfaceStyleShading(SurfaceColour=colour_rgb)
    style = ifc_file.createIfcSurfaceStyle(
        Name=surface_type,
        Side='BOTH',
        Styles=[rendering],
    )

    style_cache[surface_type] = style
    return style


def create_building_element_proxy(ifc_file, site, context, polygons, name="GML Element",
                                   surface_types=None, color_map=None, style_cache=None):
    """Create an IfcBuildingElementProxy from polygons.

    If color_map is provided, faces are grouped by surface type into separate
    IfcFaceBasedSurfaceModel items, each with its own IfcStyledItem so that
    viewers render distinct colors within a single element.
    """
    try:
        element = ifcopenshell.api.run(
            "root.create_entity",
            ifc_file,
            ifc_class="IfcBuildingElementProxy",
            name=name
        )

        origin = ifc_file.createIfcCartesianPoint((0.0, 0.0, 0.0))
        axis = ifc_file.createIfcDirection((0.0, 0.0, 1.0))
        ref_direction = ifc_file.createIfcDirection((1.0, 0.0, 0.0))
        placement_3d = ifc_file.createIfcAxis2Placement3D(origin, axis, ref_direction)
        element.ObjectPlacement = ifc_file.createIfcLocalPlacement(
            site.ObjectPlacement,
            placement_3d
        )

        ifcopenshell.api.run(
            "spatial.assign_container",
            ifc_file,
            relating_structure=site,
            products=[element]
        )

        use_colors = (color_map and surface_types and style_cache is not None)

        if use_colors:
            # Group faces by surface type → separate surface models per type
            from collections import defaultdict
            faces_by_type = defaultdict(list)

            for i, polygon_coords in enumerate(polygons):
                face = create_face_surface(ifc_file, context, polygon_coords)
                if face:
                    stype = surface_types[i] if i < len(surface_types) else 'unknown'
                    faces_by_type[stype].append(face)

            if not faces_by_type:
                return None

            # One IfcFaceBasedSurfaceModel per surface type, each styled
            rep_items = []
            for stype, faces in faces_by_type.items():
                shell = ifc_file.createIfcClosedShell(faces) if len(faces) > 2 \
                    else ifc_file.createIfcOpenShell(faces)
                surface_model = ifc_file.createIfcFaceBasedSurfaceModel([shell])

                surf_style = _get_or_create_surface_style(
                    ifc_file, style_cache, stype, color_map
                )
                ifc_file.createIfcStyledItem(surface_model, [surf_style], stype)
                rep_items.append(surface_model)
        else:
            # No coloring — single shell with all faces
            faces = []
            for polygon_coords in polygons:
                face = create_face_surface(ifc_file, context, polygon_coords)
                if face:
                    faces.append(face)

            if not faces:
                return None

            shell = ifc_file.createIfcClosedShell(faces) if len(faces) > 2 \
                else ifc_file.createIfcOpenShell(faces)
            surface_model = ifc_file.createIfcFaceBasedSurfaceModel([shell])
            rep_items = [surface_model]

        shape = ifc_file.createIfcShapeRepresentation(
            ContextOfItems=context,
            RepresentationIdentifier='Body',
            RepresentationType='SurfaceModel',
            Items=rep_items
        )

        product_shape = ifc_file.createIfcProductDefinitionShape(Representations=[shape])
        element.Representation = product_shape

        return element

    except Exception as e:
        raise Exception(f"Error creating IFC element: {e}")


# ============================================================================
# MAIN CONVERSION FUNCTION
# ============================================================================
def detect_surface_types(gml_contents):
    """Scan one or more GML byte contents and return the set of surface types found."""
    found = set()
    for content in (gml_contents if isinstance(gml_contents, list) else [gml_contents]):
        buildings = parse_buildings(content)
        for bldg in buildings:
            found.update(bldg.get('surface_types', []))
    found.discard('unknown')
    return sorted(found)


def convert_gml_to_ifc_bytes(gml_content, filename, default_epsg=25832,
                              use_map_conversion=False, boundary_polygon=None,
                              input_crs_key=None, output_crs_key=None,
                              color_map=None):
    """
    Convert GML content to IFC and return as bytes with metadata.

    Args:
        boundary_polygon: shapely Polygon to crop buildings (optional)
        input_crs_key: CRS key override for input data (e.g. "EPSG:25832")
        output_crs_key: CRS key for output coordinates (e.g. "EPSG:31467")
        color_map: dict mapping surface type -> hex color (e.g. {'RoofSurface': '#CC0000'})

    Returns:
        dict with keys: success, ifc_bytes, epsg_code, num_polygons, bounds,
                        total_buildings, kept_buildings, error
    """
    try:
        # Determine input CRS
        detected_epsg = extract_epsg_from_gml(gml_content)
        if input_crs_key:
            epsg_code = input_crs_key  # store as key string for CRS resolve
            epsg_label = input_crs_key
        elif detected_epsg:
            epsg_code = f"EPSG:{detected_epsg}"
            epsg_label = epsg_code
        else:
            epsg_code = f"EPSG:{default_epsg}"
            epsg_label = epsg_code

        # Parse buildings (grouped by bldg:Building)
        buildings = parse_buildings(gml_content)
        if not buildings:
            return {
                'success': False,
                'error': 'No buildings/polygons found in GML file'
            }

        total_buildings = len(buildings)
        kept_buildings = total_buildings

        # Crop by boundary if provided
        if boundary_polygon is not None:
            buildings, total_buildings, kept_buildings = \
                crop_buildings_by_boundary(buildings, boundary_polygon)
            if not buildings:
                return {
                    'success': False,
                    'error': f'No buildings intersect the boundary (0 of {total_buildings})'
                }

        # CRS transformation if output differs from input
        needs_transform = (output_crs_key and output_crs_key != epsg_code)
        if needs_transform:
            from_crs = resolve_crs(epsg_code)
            to_crs = resolve_crs(output_crs_key)
            buildings = transform_building_coords(buildings, from_crs, to_crs)
            ifc_epsg_label = output_crs_key
        else:
            ifc_epsg_label = epsg_code

        # Collect all polygons for bounds calculation
        all_polygons = []
        for bldg in buildings:
            all_polygons.extend(bldg['polygons'])

        bounds = get_coordinate_bounds(all_polygons)

        # Determine numeric EPSG for IFC MapConversion
        ifc_epsg_num = None
        if ifc_epsg_label.startswith("EPSG:"):
            ifc_epsg_num = int(ifc_epsg_label.split(":")[1])
        elif epsg_label.startswith("EPSG:"):
            ifc_epsg_num = int(epsg_label.split(":")[1])

        # Create IFC file
        ifc_file, site, context = create_ifc_file(ifc_epsg_num, use_map_conversion)

        # Create one IfcBuildingElementProxy per building
        element_stem = Path(filename).stem
        element_count = 0
        style_cache = {}  # shared across buildings to reuse IFC style entities
        for bldg in buildings:
            name = bldg['name'] if len(buildings) > 1 else element_stem
            element = create_building_element_proxy(
                ifc_file, site, context, bldg['polygons'], name=name,
                surface_types=bldg.get('surface_types'),
                color_map=color_map,
                style_cache=style_cache,
            )
            if element:
                element_count += 1

        if element_count == 0:
            return {
                'success': False,
                'error': 'Failed to create IFC geometry'
            }

        # Write to temporary file and read as bytes
        with tempfile.NamedTemporaryFile(suffix='.ifc', delete=False) as tmp:
            tmp_path = tmp.name

        ifc_file.write(tmp_path)

        with open(tmp_path, 'rb') as f:
            ifc_bytes = f.read()

        try:
            os.unlink(tmp_path)
        except:
            pass

        return {
            'success': True,
            'ifc_bytes': ifc_bytes,
            'epsg_code': ifc_epsg_label,
            'num_polygons': len(all_polygons),
            'num_buildings': element_count,
            'total_buildings': total_buildings,
            'kept_buildings': kept_buildings,
            'bounds': bounds
        }

    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


def convert_gml_files_merged(gml_file_list, use_map_conversion=False,
                              boundary_polygon=None, input_crs_key=None,
                              output_crs_key=None, color_map=None,
                              default_epsg=25832):
    """Convert multiple GML files into a single merged IFC.

    Args:
        gml_file_list: list of (filename, gml_bytes) tuples

    Returns:
        dict with keys: success, ifc_bytes, epsg_code, num_polygons, num_buildings,
                        total_buildings, kept_buildings, bounds, per_file_stats, error
    """
    try:
        # Collect all buildings from all files
        all_buildings = []
        per_file_stats = []
        epsg_code = None

        for filename, gml_content in gml_file_list:
            # Detect CRS from first file
            if epsg_code is None:
                detected = extract_epsg_from_gml(gml_content)
                if input_crs_key:
                    epsg_code = input_crs_key
                elif detected:
                    epsg_code = f"EPSG:{detected}"
                else:
                    epsg_code = f"EPSG:{default_epsg}"

            buildings = parse_buildings(gml_content)
            file_total = len(buildings)

            # Tag buildings with source filename for naming
            stem = Path(filename).stem
            for i, bldg in enumerate(buildings):
                if not bldg['name'] or bldg['name'] == 'GML Element':
                    bldg['name'] = f'{stem}_{i+1}'

            all_buildings.extend(buildings)
            per_file_stats.append({'file': filename, 'buildings': file_total})

        if not all_buildings:
            return {'success': False, 'error': 'No buildings found in any file'}

        total_buildings = len(all_buildings)
        kept_buildings = total_buildings

        # Crop by boundary
        if boundary_polygon is not None:
            all_buildings, total_buildings, kept_buildings = \
                crop_buildings_by_boundary(all_buildings, boundary_polygon)
            if not all_buildings:
                return {
                    'success': False,
                    'error': f'No buildings intersect the boundary (0 of {total_buildings})'
                }

        # CRS transformation
        needs_transform = (output_crs_key and output_crs_key != epsg_code)
        if needs_transform:
            from_crs = resolve_crs(epsg_code)
            to_crs = resolve_crs(output_crs_key)
            all_buildings = transform_building_coords(all_buildings, from_crs, to_crs)
            ifc_epsg_label = output_crs_key
        else:
            ifc_epsg_label = epsg_code

        # Collect polygons for bounds
        all_polygons = []
        for bldg in all_buildings:
            all_polygons.extend(bldg['polygons'])
        bounds = get_coordinate_bounds(all_polygons)

        # Numeric EPSG for MapConversion
        ifc_epsg_num = None
        if ifc_epsg_label.startswith("EPSG:"):
            ifc_epsg_num = int(ifc_epsg_label.split(":")[1])

        # Create IFC
        ifc_file, site, context = create_ifc_file(ifc_epsg_num, use_map_conversion)

        style_cache = {}
        element_count = 0
        for bldg in all_buildings:
            element = create_building_element_proxy(
                ifc_file, site, context, bldg['polygons'], name=bldg['name'],
                surface_types=bldg.get('surface_types'),
                color_map=color_map,
                style_cache=style_cache,
            )
            if element:
                element_count += 1

        if element_count == 0:
            return {'success': False, 'error': 'Failed to create IFC geometry'}

        # Write to temp and read bytes
        with tempfile.NamedTemporaryFile(suffix='.ifc', delete=False) as tmp:
            tmp_path = tmp.name
        ifc_file.write(tmp_path)
        with open(tmp_path, 'rb') as f:
            ifc_bytes = f.read()
        try:
            os.unlink(tmp_path)
        except:
            pass

        return {
            'success': True,
            'ifc_bytes': ifc_bytes,
            'epsg_code': ifc_epsg_label,
            'num_polygons': len(all_polygons),
            'num_buildings': element_count,
            'total_buildings': total_buildings,
            'kept_buildings': kept_buildings,
            'bounds': bounds,
            'per_file_stats': per_file_stats,
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}