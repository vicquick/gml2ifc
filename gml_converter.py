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

# ============================================================================
# GML NAMESPACES
# ============================================================================
NAMESPACES = {
    'gml': 'http://www.opengis.net/gml',
    'gml32': 'http://www.opengis.net/gml/3.2',
    'citygml': 'http://www.opengis.net/citygml/2.0',
    'bldg': 'http://www.opengis.net/citygml/building/2.0',
}


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


def create_building_element_proxy(ifc_file, site, context, polygons, name="GML Element"):
    """Create an IfcBuildingElementProxy from polygons"""
    try:
        # Create the building element proxy
        element = ifcopenshell.api.run(
            "root.create_entity",
            ifc_file,
            ifc_class="IfcBuildingElementProxy",
            name=name
        )
        
        # Create local placement for the element (at origin, relative to site)
        origin = ifc_file.createIfcCartesianPoint((0.0, 0.0, 0.0))
        axis = ifc_file.createIfcDirection((0.0, 0.0, 1.0))
        ref_direction = ifc_file.createIfcDirection((1.0, 0.0, 0.0))
        placement_3d = ifc_file.createIfcAxis2Placement3D(origin, axis, ref_direction)
        element.ObjectPlacement = ifc_file.createIfcLocalPlacement(
            site.ObjectPlacement,
            placement_3d
        )
        
        # Assign to site
        ifcopenshell.api.run(
            "spatial.assign_container",
            ifc_file,
            relating_structure=site,
            products=[element]
        )
        
        # Create faces from all polygons
        faces = []
        for polygon_coords in polygons:
            face = create_face_surface(ifc_file, context, polygon_coords)
            if face:
                faces.append(face)
        
        if not faces:
            return None
        
        # Create shell from faces
        shell = ifc_file.createIfcClosedShell(faces) if len(faces) > 2 else ifc_file.createIfcOpenShell(faces)
        
        # Create face based surface model
        surface_model = ifc_file.createIfcFaceBasedSurfaceModel([shell])
        
        # Create shape representation
        shape = ifc_file.createIfcShapeRepresentation(
            ContextOfItems=context,
            RepresentationIdentifier='Body',
            RepresentationType='SurfaceModel',
            Items=[surface_model]
        )
        
        # Create product definition shape
        product_shape = ifc_file.createIfcProductDefinitionShape(Representations=[shape])
        element.Representation = product_shape
        
        return element
            
    except Exception as e:
        raise Exception(f"Error creating IFC element: {e}")


# ============================================================================
# MAIN CONVERSION FUNCTION
# ============================================================================
def convert_gml_to_ifc_bytes(gml_content, filename, default_epsg=25832, use_map_conversion=False):
    """
    Convert GML content to IFC and return as bytes with metadata
    
    Returns:
        dict with keys: success, ifc_bytes, epsg_code, num_polygons, bounds, error
    """
    try:
        # Extract EPSG code
        epsg_code = extract_epsg_from_gml(gml_content)
        if not epsg_code:
            epsg_code = default_epsg
        
        # Parse polygons
        polygons = parse_gml_polygons(gml_content)
        if not polygons:
            return {
                'success': False,
                'error': 'No polygons found in GML file'
            }
        
        # Get coordinate bounds
        bounds = get_coordinate_bounds(polygons)
        
        # Create IFC file
        ifc_file, site, context = create_ifc_file(epsg_code, use_map_conversion)
        
        # Create building element proxy
        element_name = Path(filename).stem
        element = create_building_element_proxy(
            ifc_file, site, context, polygons, name=element_name
        )
        
        if not element:
            return {
                'success': False,
                'error': 'Failed to create IFC geometry'
            }
        
        # Write to temporary file and read as bytes
        with tempfile.NamedTemporaryFile(suffix='.ifc', delete=False) as tmp:
            tmp_path = tmp.name
        
        # Write the file (after closing the file handle)
        ifc_file.write(tmp_path)
        
        # Read the bytes
        with open(tmp_path, 'rb') as f:
            ifc_bytes = f.read()
        
        # Delete the temporary file
        try:
            os.unlink(tmp_path)
        except:
            pass  # Ignore if file can't be deleted
        
        return {
            'success': True,
            'ifc_bytes': ifc_bytes,
            'epsg_code': epsg_code,
            'num_polygons': len(polygons),
            'bounds': bounds
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }