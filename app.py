#!/usr/bin/env python3
"""
GeoData Converter - Streamlit App
Web interface for converting geospatial data formats
"""

import streamlit as st
from pathlib import Path
import io
import zipfile

# Import converter functions
from gml_converter import (
    convert_gml_to_ifc_bytes, convert_gml_files_merged,
    CRS_OPTIONS, DEFAULT_SURFACE_COLORS, detect_surface_types,
)
from tif_converter import (
    extract_tif_metadata,
    convert_tif_to_shapefile
)

# ============================================================================
# PAGE CONFIGURATION
# ============================================================================
st.set_page_config(
    page_title="GeoData Converter",
    page_icon="🗺️",
    layout="centered"
)

# ============================================================================
# HEADER
# ============================================================================
st.title("🗺️ GeoData Converter")
st.caption("powered by Streamlit + IfcOpenShell + pyproj")
st.markdown("Convert geospatial data between formats")

st.divider()

# ============================================================================
# TABS
# ============================================================================
tab1, tab2 = st.tabs(["🏗️ GML → IFC", "📏 TIF → 3D Contour SHP"])

# ============================================================================
# TAB 1: GML TO IFC
# ============================================================================
with tab1:
    st.subheader("GML to IFC4X3 Converter")
    st.markdown("Convert GML (Geography Markup Language) files to IFC4X3 format")
    
    # SETTINGS
    with st.expander("⚙️ Settings"):
        use_map_conversion = st.checkbox(
            "Enable MapConversion",
            value=False,
            help="Enable IfcMapConversion for georeferencing (may cause coordinate issues in some viewers)",
            key="gml_map_conversion"
        )

        # CRS selectors
        crs_labels = [f"{code} — {name}" for code, name in CRS_OPTIONS.items()]

        input_crs_label = st.selectbox(
            "Input CRS",
            ["Auto-detect from GML"] + crs_labels,
            index=0,
            help="Override CRS if GML file has no srsName attribute",
            key="gml_input_crs"
        )
        input_crs_key = None
        if input_crs_label != "Auto-detect from GML":
            input_crs_key = input_crs_label.split(" — ")[0]

        output_crs_label = st.selectbox(
            "Output CRS",
            crs_labels,
            index=0,  # default EPSG:25832
            help="Target coordinate reference system for IFC output",
            key="gml_output_crs"
        )
        output_crs_key = output_crs_label.split(" — ")[0]

        st.divider()

        # Boundary crop uploader
        boundary_file = st.file_uploader(
            "Upload boundary SHP (optional — crop buildings to area)",
            type=['zip'],
            help="ZIP containing a Shapefile (.shp/.shx/.dbf/.prj) to crop buildings to a bounding area",
            key="gml_boundary"
        )

        boundary_polygon = None
        if boundary_file:
            try:
                import geopandas as gpd
                import tempfile as _tmpmod
                import zipfile as _zipmod

                boundary_bytes = boundary_file.read()
                with _tmpmod.TemporaryDirectory() as tmpdir:
                    zip_path = Path(tmpdir) / "boundary.zip"
                    zip_path.write_bytes(boundary_bytes)

                    with _zipmod.ZipFile(zip_path, 'r') as zf:
                        zf.extractall(tmpdir)

                    shp_files = list(Path(tmpdir).glob("**/*.shp"))
                    if not shp_files:
                        st.error("No .shp file found in the uploaded ZIP")
                    else:
                        gdf = gpd.read_file(shp_files[0])
                        boundary_polygon = gdf.union_all()
                        st.success(f"Boundary loaded: {len(gdf)} feature(s)")
            except Exception as e:
                st.error(f"Error reading boundary SHP: {e}")

        st.divider()

        # Merge option
        merge_files = st.checkbox(
            "Merge all GML files into single IFC",
            value=False,
            help="Combine all uploaded GML files into one merged IFC instead of separate files",
            key="gml_merge"
        )

        st.divider()

        # Color by surface type
        enable_colors = st.checkbox(
            "Color by surface type",
            value=False,
            help="Apply colors to roof, wall, and ground surfaces (CityGML LoD2+)",
            key="gml_enable_colors"
        )

        color_map = None
        split_by_surface = False
        if enable_colors:
            st.caption("Customize colors per surface type:")
            color_map = {}
            for stype, default_hex in DEFAULT_SURFACE_COLORS.items():
                if stype == 'unknown':
                    label = "Other / unclassified"
                else:
                    label = stype.replace('Surface', ' Surface')
                color_map[stype] = st.color_picker(
                    label, value=default_hex, key=f"color_{stype}"
                )
            split_by_surface = st.checkbox(
                "Split elements by surface type (Vectorworks compatibility)",
                value=False,
                help="Creates separate IFC elements per surface type (Roof, Wall, Ground) so each gets its own color in Vectorworks",
                key="gml_split_surface"
            )

    st.divider()

    # FILE UPLOAD
    st.subheader("📂 Upload GML File(s)")

    uploaded_files = st.file_uploader(
        "Select one or more GML files",
        type=['gml', 'GML'],
        accept_multiple_files=True,
        help="Upload GML files containing polygon geometry",
        key="gml_uploader"
    )

    # PROCESSING
    if uploaded_files:
        st.divider()
        st.subheader(f"🔄 Processing {len(uploaded_files)} file(s)")

        # Store converted files
        converted_files = {}

        if merge_files and len(uploaded_files) > 1:
            # ── MERGED MODE ──
            info_placeholder = st.empty()
            try:
                with st.spinner("Merging and converting..."):
                    gml_file_list = []
                    for uf in uploaded_files:
                        gml_file_list.append((uf.name, uf.read()))

                    result = convert_gml_files_merged(
                        gml_file_list=gml_file_list,
                        use_map_conversion=use_map_conversion,
                        boundary_polygon=boundary_polygon,
                        input_crs_key=input_crs_key,
                        output_crs_key=output_crs_key,
                        color_map=color_map,
                        split_by_surface=split_by_surface,
                    )

                    if result['success']:
                        output_filename = "merged_output.ifc"
                        converted_files[output_filename] = result['ifc_bytes']

                        msg_parts = ["✓ Merged"]
                        if result.get('kept_buildings', 0) < result.get('total_buildings', 0):
                            msg_parts.append(f"kept {result['kept_buildings']} of {result['total_buildings']} buildings")
                        else:
                            msg_parts.append(f"{result.get('num_buildings', '?')} building(s)")
                        msg_parts.append(f"{result['num_polygons']} polygon(s)")
                        msg_parts.append(f"{result['epsg_code']}")
                        info_placeholder.success(" • ".join(msg_parts))

                        if result.get('bounds'):
                            bounds = result['bounds']
                            with st.expander("📍 Coordinate Bounds", expanded=False):
                                col_min, col_max = st.columns(2)
                                with col_min:
                                    st.metric("Min X", f"{bounds['min'][0]:.2f}")
                                    st.metric("Min Y", f"{bounds['min'][1]:.2f}")
                                    st.metric("Min Z", f"{bounds['min'][2]:.2f}")
                                with col_max:
                                    st.metric("Max X", f"{bounds['max'][0]:.2f}")
                                    st.metric("Max Y", f"{bounds['max'][1]:.2f}")
                                    st.metric("Max Z", f"{bounds['max'][2]:.2f}")
                    else:
                        info_placeholder.error(f"✗ Failed: {result.get('error', 'Unknown error')}")

            except Exception as e:
                info_placeholder.error(f"✗ Error: {str(e)}")

            st.divider()
        else:
            # ── PER-FILE MODE ──
            for idx, uploaded_file in enumerate(uploaded_files, 1):
                with st.container():
                    col1, col2 = st.columns([3, 1])

                    with col1:
                        st.write(f"**{idx}. {uploaded_file.name}**")

                    with col2:
                        file_size = len(uploaded_file.getvalue()) / 1024
                        st.caption(f"{file_size:.1f} KB")

                    info_placeholder = st.empty()

                    try:
                        with st.spinner(f"Converting..."):
                            gml_content = uploaded_file.read()

                            result = convert_gml_to_ifc_bytes(
                                gml_content=gml_content,
                                filename=uploaded_file.name,
                                use_map_conversion=use_map_conversion,
                                boundary_polygon=boundary_polygon,
                                input_crs_key=input_crs_key,
                                output_crs_key=output_crs_key,
                                color_map=color_map,
                                split_by_surface=split_by_surface,
                            )

                            if result['success']:
                                output_filename = Path(uploaded_file.name).stem + ".ifc"
                                converted_files[output_filename] = result['ifc_bytes']

                                msg_parts = ["✓ Converted"]
                                if result.get('total_buildings') and result.get('kept_buildings') is not None:
                                    if result['kept_buildings'] < result['total_buildings']:
                                        msg_parts.append(f"kept {result['kept_buildings']} of {result['total_buildings']} buildings")
                                    else:
                                        msg_parts.append(f"{result.get('num_buildings', '?')} building(s)")
                                msg_parts.append(f"{result['num_polygons']} polygon(s)")
                                msg_parts.append(f"{result['epsg_code']}")
                                info_placeholder.success(" • ".join(msg_parts))

                                if result.get('bounds'):
                                    bounds = result['bounds']
                                    with st.expander("📍 Coordinate Bounds", expanded=False):
                                        col_min, col_max = st.columns(2)
                                        with col_min:
                                            st.metric("Min X", f"{bounds['min'][0]:.2f}")
                                            st.metric("Min Y", f"{bounds['min'][1]:.2f}")
                                            st.metric("Min Z", f"{bounds['min'][2]:.2f}")
                                        with col_max:
                                            st.metric("Max X", f"{bounds['max'][0]:.2f}")
                                            st.metric("Max Y", f"{bounds['max'][1]:.2f}")
                                            st.metric("Max Z", f"{bounds['max'][2]:.2f}")
                            else:
                                info_placeholder.error(f"✗ Failed: {result.get('error', 'Unknown error')}")

                    except Exception as e:
                        info_placeholder.error(f"✗ Error: {str(e)}")

                st.divider()
        
        # DOWNLOAD SECTION
        if converted_files:
            st.subheader("📥 Download Results")
            
            if len(converted_files) == 1:
                # Single file download
                filename, file_bytes = list(converted_files.items())[0]
                
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.download_button(
                        label=f"⬇️ Download {filename}",
                        data=file_bytes,
                        file_name=filename,
                        mime="application/x-step",
                        use_container_width=True
                    )
                with col2:
                    size_kb = len(file_bytes) / 1024
                    st.metric("Size", f"{size_kb:.1f} KB")
            
            else:
                # Multiple files - create zip
                zip_buffer = io.BytesIO()
                total_size = 0
                
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for filename, file_bytes in converted_files.items():
                        zip_file.writestr(filename, file_bytes)
                        total_size += len(file_bytes)
                
                zip_buffer.seek(0)
                zip_size_kb = len(zip_buffer.getvalue()) / 1024
                
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.download_button(
                        label=f"⬇️ Download All ({len(converted_files)} files as ZIP)",
                        data=zip_buffer.getvalue(),
                        file_name="gml_to_ifc_conversion.zip",
                        mime="application/zip",
                        use_container_width=True
                    )
                with col2:
                    st.metric("ZIP Size", f"{zip_size_kb:.1f} KB")
                
                # Individual file downloads
                with st.expander("📄 Download Individual Files", expanded=False):
                    for filename, file_bytes in converted_files.items():
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.download_button(
                                label=filename,
                                data=file_bytes,
                                file_name=filename,
                                mime="application/x-step",
                                key=filename,
                                use_container_width=True
                            )
                        with col2:
                            size_kb = len(file_bytes) / 1024
                            st.caption(f"{size_kb:.1f} KB")

    else:
        st.info("👆 Upload one or more GML files to get started")
        
        with st.expander("ℹ️ About this converter"):
            st.markdown("""
            This tool converts GML (Geography Markup Language) files to IFC4X3 format:
            
            **Features:**
            - Automatic EPSG coordinate system detection
            - Support for polygon geometry with holes
            - Batch processing of multiple files
            - Creates IfcBuildingElementProxy objects
            - Proper 3D surface model generation
            
            **Input Requirements:**
            - GML files with polygon geometry
            - Supported: exterior and interior rings
            - 2D or 3D coordinates
            
            **Output:**
            - IFC4X3 format
            - IfcBuildingElementProxy with surface geometry
            - EPSG coordinate reference system (optional)
            """)


# ============================================================================
# TAB 2: TIF TO 3D CONTOUR SHAPEFILE
# ============================================================================
with tab2:
    st.subheader("TIF to 3D Contour Shapefile")
    st.markdown("Generate 3D contour polylines from elevation GeoTIFF (DGM1) and export as Shapefile")
    
    # FILE UPLOAD
    st.subheader("📂 Upload TIF File")
    
    uploaded_tif = st.file_uploader(
        "Select a GeoTIFF elevation file",
        type=['tif', 'tiff', 'TIF', 'TIFF'],
        help="Upload a GeoTIFF file containing elevation data (e.g., DGM1)",
        key="tif_uploader"
    )
    
    if uploaded_tif:
        # Read TIF bytes
        tif_bytes = uploaded_tif.read()
        file_size = len(tif_bytes) / (1024 * 1024)  # MB
        
        st.divider()
        
        # Extract and display metadata
        with st.spinner("Analyzing GeoTIFF..."):
            metadata_result = extract_tif_metadata(tif_bytes)
        
        if metadata_result['success']:
            st.success("✓ TIF file loaded successfully")
            
            # Display metadata
            with st.expander("📊 TIF Metadata", expanded=True):
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    st.metric("Resolution", f"{metadata_result['resolution']:.3f} m")
                    st.metric("Width", f"{metadata_result['width']} px")
                
                with col2:
                    st.metric("EPSG Code", metadata_result['epsg_code'] or "Unknown")
                    st.metric("Height", f"{metadata_result['height']} px")
                
                with col3:
                    st.metric("File Size", f"{file_size:.1f} MB")
                
                st.markdown("**Elevation Range:**")
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Min", f"{metadata_result['elevation_range']['min']:.2f} m")
                with col2:
                    st.metric("Mean", f"{metadata_result['elevation_range']['mean']:.2f} m")
                with col3:
                    st.metric("Max", f"{metadata_result['elevation_range']['max']:.2f} m")
                
                st.markdown("**Bounds:**")
                bounds = metadata_result['bounds']
                st.caption(f"Left: {bounds['left']:.2f} | Bottom: {bounds['bottom']:.2f} | Right: {bounds['right']:.2f} | Top: {bounds['top']:.2f}")
            
            st.divider()
            
            # SETTINGS
            st.subheader("⚙️ Contour Generation Settings")
            
            col1, col2 = st.columns(2)
            
            with col1:
                # Auto-calculate suggested interval
                res = metadata_result['resolution']
                if res < 1:
                    suggested_interval = 0.5
                elif res < 5:
                    suggested_interval = 1.0
                elif res < 10:
                    suggested_interval = 5.0
                else:
                    suggested_interval = 10.0
                
                interval = st.number_input(
                    "Contour Interval (m)",
                    min_value=0.1,
                    max_value=100.0,
                    value=float(suggested_interval),
                    step=0.5,
                    help=f"Vertical spacing between contour lines (suggested: {suggested_interval}m based on resolution)",
                    key="contour_interval"
                )
            
            with col2:
                use_elevation_filter = st.checkbox(
                    "Filter Elevation Range",
                    value=False,
                    help="Limit contour generation to specific elevation range",
                    key="use_elevation_filter"
                )
            
            if use_elevation_filter:
                col1, col2 = st.columns(2)
                with col1:
                    min_elev = st.number_input(
                        "Min Elevation (m)",
                        value=float(metadata_result['elevation_range']['min']),
                        min_value=float(metadata_result['elevation_range']['min']),
                        max_value=float(metadata_result['elevation_range']['max']),
                        key="min_elevation"
                    )
                with col2:
                    max_elev = st.number_input(
                        "Max Elevation (m)",
                        value=float(metadata_result['elevation_range']['max']),
                        min_value=float(metadata_result['elevation_range']['min']),
                        max_value=float(metadata_result['elevation_range']['max']),
                        key="max_elevation"
                    )
            else:
                min_elev = None
                max_elev = None
            
            # Simplification settings
            st.markdown("**Simplification Algorithm:**")
            
            simplification_method = st.selectbox(
                "Method",
                options=['none', 'douglas-peucker', 'chaikin'],
                format_func=lambda x: {
                    'none': 'None (Original)',
                    'douglas-peucker': 'Douglas-Peucker (Reduce Points)',
                    'chaikin': 'Chaikin (Corner Smoothing)'
                }[x],
                help="Choose algorithm to simplify contour lines",
                key="simplification_method"
            )
            
            simplification_params = {}
            
            if simplification_method == 'douglas-peucker':
                tolerance = st.slider(
                    "Tolerance (m)",
                    min_value=0.1,
                    max_value=10.0,
                    value=1.0,
                    step=0.1,
                    help="Maximum distance between original and simplified line (higher = more simplification)",
                    key="dp_tolerance"
                )
                simplification_params['tolerance'] = tolerance
            
            elif simplification_method == 'chaikin':
                iterations = st.slider(
                    "Iterations",
                    min_value=1,
                    max_value=5,
                    value=2,
                    step=1,
                    help="Number of smoothing passes (higher = smoother curves)",
                    key="chaikin_iterations"
                )
                simplification_params['iterations'] = iterations
            
            st.divider()
            
            # GENERATE BUTTON
            if st.button("🔄 Generate Contours", type="primary", use_container_width=True):
                with st.spinner("Generating contours... This may take a while for large files."):
                    result = convert_tif_to_shapefile(
                        tif_bytes=tif_bytes,
                        filename=uploaded_tif.name,
                        interval=interval,
                        min_elevation=min_elev,
                        max_elevation=max_elev,
                        simplification_method=simplification_method,
                        simplification_params=simplification_params
                    )
                
                if result['success']:
                    st.success(f"✓ Successfully generated {result['metadata']['num_contours']} contour lines!")
                    
                    # Display generation info
                    with st.expander("📊 Generation Details", expanded=True):
                        col1, col2, col3 = st.columns(3)
                        
                        with col1:
                            st.metric("Contours", result['metadata']['num_contours'])
                            st.metric("Interval", f"{result['metadata']['interval']} m")
                        
                        with col2:
                            st.metric("EPSG Code", result['metadata']['epsg_code'] or "Unknown")
                            st.metric("Resolution", f"{result['metadata']['resolution']:.3f} m")
                        
                        with col3:
                            st.metric("Simplification", result['metadata']['simplification'].replace('-', ' ').title())
                        
                        st.markdown("**Contour Levels:**")
                        levels = result['metadata']['contour_levels']
                        st.caption(f"{len(levels)} levels from {min(levels):.1f}m to {max(levels):.1f}m")
                    
                    st.divider()
                    
                    # DOWNLOAD SECTION
                    st.subheader("📥 Download Shapefile")
                    
                    zip_size_kb = len(result['zip_bytes']) / 1024
                    
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.download_button(
                            label=f"⬇️ Download {result['filename']}",
                            data=result['zip_bytes'],
                            file_name=result['filename'],
                            mime="application/zip",
                            use_container_width=True,
                            key="download_shapefile"
                        )
                    with col2:
                        st.metric("Size", f"{zip_size_kb:.1f} KB")
                    
                    st.info("💡 The ZIP contains all shapefile components (.shp, .shx, .dbf, .prj). Extract all files to the same directory to use the shapefile.")
                
                else:
                    st.error(f"✗ Failed to generate contours: {result.get('error', 'Unknown error')}")
        
        else:
            st.error(f"✗ Failed to read TIF file: {metadata_result.get('error', 'Unknown error')}")
    
    else:
        st.info("👆 Upload a GeoTIFF file to get started")
        
        with st.expander("ℹ️ About this converter"):
            st.markdown("""
            This tool generates 3D contour polylines from elevation GeoTIFF files:
            
            **Features:**
            - Automatic resolution detection and interval suggestion
            - Support for DGM1 and other elevation rasters
            - 3D polylines (PolyLineZ) with elevation as Z coordinate
            - Multiple simplification algorithms
            - EPSG coordinate reference system preservation
            
            **Simplification Algorithms:**
            - **Douglas-Peucker**: Reduces number of points while preserving shape
            - **Chaikin**: Smooths corners and curves through iterative refinement
            
            **Input Requirements:**
            - GeoTIFF with elevation data (single band)
            - Coordinate reference system (CRS) metadata
            
            **Output:**
            - ESRI Shapefile (as ZIP)
            - 3D polyline features (PolyLineZ)
            - Attributes: ELEVATION, CONTOUR_ID
            - All standard shapefile components (.shp, .shx, .dbf, .prj)
            """)


# ============================================================================
# FOOTER
# ============================================================================
st.divider()
st.markdown(
    """
    <div style='text-align: center; color: gray; font-size: 0.8em; padding: 1em 0;'>
    GeoData Converter v1.1 | Built with Streamlit, IfcOpenShell & pyproj
    </div>
    """,
    unsafe_allow_html=True
)