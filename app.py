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
    resolve_crs,
)
from tif_converter import (
    extract_tif_metadata,
    convert_tif_to_shapefile,
    suggest_contour_interval,
)
from wms_fetcher import (
    get_wms_layers,
    fetch_wms_elevation_tif,
    extract_shapefile_bbox
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
    st.markdown("Generate 3D contour polylines from elevation data and export as Shapefile")

    # Input mode selector
    input_mode = st.radio(
        "Input Source",
        ["Upload TIF", "Fetch from WMS"],
        horizontal=True,
        key="tif_input_mode"
    )

    # Clear WMS session state when switching to Upload mode
    if input_mode == "Upload TIF":
        for key in ['wms_layers', 'wms_tif_bytes', 'wms_source_filename']:
            if key in st.session_state:
                del st.session_state[key]

    tif_bytes = None
    source_filename = None

    # ── UPLOAD TIF BRANCH ──
    if input_mode == "Upload TIF":
        st.subheader("Upload TIF File")

        uploaded_tif = st.file_uploader(
            "Select a GeoTIFF elevation file",
            type=['tif', 'tiff', 'TIF', 'TIFF'],
            help="Upload a GeoTIFF file containing elevation data (e.g., DGM1)",
            key="tif_uploader"
        )

        if uploaded_tif:
            tif_bytes = uploaded_tif.read()
            source_filename = uploaded_tif.name

    # ── FETCH FROM WMS BRANCH ──
    else:
        st.subheader("Fetch Elevation from WMS")

        wms_url = st.text_input(
            "WMS URL",
            placeholder="https://example.com/wms",
            help="Base URL of the WMS service (without query parameters)",
            key="wms_url"
        )

        # Load Layers button
        if wms_url and st.button("Load Layers", key="wms_load_layers"):
            with st.spinner("Fetching WMS capabilities..."):
                caps_result = get_wms_layers(wms_url)
            if caps_result['success']:
                if caps_result['layers']:
                    st.session_state['wms_layers'] = caps_result['layers']
                    st.success(f"Found {len(caps_result['layers'])} layer(s)")
                else:
                    st.warning("WMS returned no layers")
                    st.session_state.pop('wms_layers', None)
            else:
                st.error(f"Failed to load layers: {caps_result['error']}")
                st.session_state.pop('wms_layers', None)

        # Layer selector (persisted in session state)
        if 'wms_layers' in st.session_state and st.session_state['wms_layers']:
            layers = st.session_state['wms_layers']
            layer_options = [f"{l['name']} — {l['title']}" for l in layers]

            selected_layer_label = st.selectbox(
                "Layer",
                options=layer_options,
                key="wms_layer_select"
            )
            selected_layer_name = layers[layer_options.index(selected_layer_label)]['name']

            st.divider()

            # Shapefile upload for bounding box
            st.markdown("**Bounding Box from Shapefile:**")
            bbox_shp = st.file_uploader(
                "Upload a shapefile (ZIP) to define the area",
                type=['zip'],
                help="ZIP containing a Shapefile (.shp/.shx/.dbf/.prj). The bounding box of all features will be used.",
                key="wms_bbox_shp"
            )

            if bbox_shp:
                shp_bytes = bbox_shp.read()
                bbox_result = extract_shapefile_bbox(shp_bytes)

                if bbox_result['success']:
                    bbox = bbox_result['bbox']
                    epsg_code = bbox_result['epsg_code']

                    st.success(f"Bounding box extracted from {bbox_result['num_features']} feature(s)")
                    st.caption(
                        f"MinX: {bbox[0]:.2f} | MinY: {bbox[1]:.2f} | "
                        f"MaxX: {bbox[2]:.2f} | MaxY: {bbox[3]:.2f}"
                    )

                    # Handle missing CRS
                    if epsg_code is None:
                        st.warning("Shapefile has no CRS. Please enter the EPSG code manually.")
                        epsg_code = st.number_input(
                            "EPSG Code",
                            min_value=1,
                            value=25832,
                            step=1,
                            help="EPSG code for the shapefile coordinate system (e.g., 25832 for ETRS89 / UTM zone 32N)",
                            key="wms_manual_epsg"
                        )
                    else:
                        st.caption(f"CRS: EPSG:{epsg_code}")

                    st.divider()

                    # Fetch button
                    if st.button("Fetch Elevation", type="primary", use_container_width=True, key="wms_fetch"):
                        with st.spinner("Fetching elevation data from WMS..."):
                            fetch_result = fetch_wms_elevation_tif(
                                wms_url=wms_url,
                                layer_name=selected_layer_name,
                                bbox=bbox,
                                crs_epsg=int(epsg_code),
                            )

                        if fetch_result['success']:
                            st.session_state['wms_tif_bytes'] = fetch_result['tif_bytes']
                            st.session_state['wms_source_filename'] = f"wms_{selected_layer_name}.tif"
                            st.success(
                                f"Fetched {len(fetch_result['tif_bytes']) / 1024:.0f} KB "
                                f"({fetch_result['width']}x{fetch_result['height']} px)"
                            )
                        else:
                            st.error(f"Failed to fetch elevation: {fetch_result['error']}")
                            st.session_state.pop('wms_tif_bytes', None)

                else:
                    st.error(f"Failed to read shapefile: {bbox_result['error']}")

        # Use WMS result if available
        if 'wms_tif_bytes' in st.session_state:
            tif_bytes = st.session_state['wms_tif_bytes']
            source_filename = st.session_state.get('wms_source_filename', 'wms_elevation.tif')

    # ── SHARED SECTION: metadata, settings, generation ──
    if tif_bytes is not None:
        file_size = len(tif_bytes) / (1024 * 1024)  # MB

        st.divider()

        # Extract and display metadata
        with st.spinner("Analyzing GeoTIFF..."):
            metadata_result = extract_tif_metadata(tif_bytes)

        if metadata_result['success']:
            st.success("TIF data loaded successfully")

            # Display metadata
            with st.expander("TIF Metadata", expanded=True):
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
            st.subheader("Contour Generation Settings")

            # Smart interval suggestion based on resolution + elevation range
            suggested_interval = suggest_contour_interval(
                metadata_result['resolution'],
                metadata_result['elevation_range']['min'],
                metadata_result['elevation_range']['max'],
            )
            elev_span = metadata_result['elevation_range']['max'] - metadata_result['elevation_range']['min']
            est_contours = int(elev_span / suggested_interval) if suggested_interval > 0 else 0

            col1, col2 = st.columns(2)

            with col1:
                interval = st.number_input(
                    "Contour Interval (m)",
                    min_value=0.1,
                    max_value=100.0,
                    value=float(suggested_interval),
                    step=0.5,
                    help=f"Auto-suggested: {suggested_interval}m (~{est_contours} contours for {elev_span:.1f}m range at {metadata_result['resolution']:.2f}m resolution)",
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

            # Output CRS selector (same options as GML pipeline)
            st.markdown("**Output CRS:**")
            _contour_crs_labels = [f"{code} — {name}" for code, name in CRS_OPTIONS.items()]
            output_crs_label = st.selectbox(
                "Output coordinate system",
                ["Same as input"] + _contour_crs_labels,
                index=0,
                help=f"Input CRS: EPSG:{metadata_result['epsg_code'] or '?'}. Choose a different output CRS to reproject contours.",
                key="output_crs"
            )
            if output_crs_label == "Same as input":
                output_crs = None
            else:
                output_crs_key = output_crs_label.split(" — ")[0]  # e.g. "EPSG:25832" or "LS320"
                output_crs = resolve_crs(output_crs_key)

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
            if st.button("Generate Contours", type="primary", use_container_width=True):
                with st.spinner("Generating contours... This may take a while for large files."):
                    result = convert_tif_to_shapefile(
                        tif_bytes=tif_bytes,
                        filename=source_filename,
                        interval=interval,
                        min_elevation=min_elev,
                        max_elevation=max_elev,
                        simplification_method=simplification_method,
                        simplification_params=simplification_params,
                        output_crs=output_crs,
                    )

                if result['success']:
                    st.success(f"Successfully generated {result['metadata']['num_contours']} contour lines!")

                    # Display generation info
                    with st.expander("Generation Details", expanded=True):
                        col1, col2, col3 = st.columns(3)

                        with col1:
                            st.metric("Contours", result['metadata']['num_contours'])
                            st.metric("Interval", f"{result['metadata']['interval']} m")

                        with col2:
                            out_crs = result['metadata'].get('output_crs_name')
                            src_epsg = result['metadata']['epsg_code']
                            if out_crs and out_crs != str(src_epsg):
                                st.metric("Output CRS", out_crs)
                                st.caption(f"(reprojected from EPSG:{src_epsg})")
                            else:
                                st.metric("EPSG Code", src_epsg or "Unknown")
                            st.metric("Resolution", f"{result['metadata']['resolution']:.3f} m")

                        with col3:
                            st.metric("Simplification", result['metadata']['simplification'].replace('-', ' ').title())

                        st.markdown("**Contour Levels:**")
                        levels = result['metadata']['contour_levels']
                        st.caption(f"{len(levels)} levels from {min(levels):.1f}m to {max(levels):.1f}m")

                    st.divider()

                    # DOWNLOAD SECTION
                    st.subheader("Download Shapefile")

                    zip_size_kb = len(result['zip_bytes']) / 1024

                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.download_button(
                            label=f"Download {result['filename']}",
                            data=result['zip_bytes'],
                            file_name=result['filename'],
                            mime="application/zip",
                            use_container_width=True,
                            key="download_shapefile"
                        )
                    with col2:
                        st.metric("Size", f"{zip_size_kb:.1f} KB")

                    st.info("The ZIP contains all shapefile components (.shp, .shx, .dbf, .prj). Extract all files to the same directory to use the shapefile.")

                else:
                    st.error(f"Failed to generate contours: {result.get('error', 'Unknown error')}")

        else:
            st.error(f"Failed to read TIF file: {metadata_result.get('error', 'Unknown error')}")

    elif input_mode == "Upload TIF":
        st.info("Upload a GeoTIFF file to get started")

        with st.expander("About this converter"):
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