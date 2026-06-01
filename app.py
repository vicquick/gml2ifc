#!/usr/bin/env python3
"""
GeoData Converter - Streamlit App
Web interface for converting geospatial data formats
"""

import streamlit as st
from pathlib import Path
import io
import zipfile
import folium
from streamlit_folium import st_folium

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
    convert_tif_to_dtm_points,
    convert_tif_to_combined_dtm,
)
from wms_fetcher import (
    get_wms_layers,
    fetch_wms_elevation_tif,
    extract_shapefile_bbox
)
from format_converter import (
    analyze_xyz,
    convert_xyz_to_tif,
    convert_xyz_zip_to_tif_zip,
    convert_xml_to_gml,
)
from tile_downloader import (
    parse_tile_index,
    kachel_center_latlon,
    kachel_from_latlon,
    get_grid_cells,
    grid_kachels,
    build_view_geojson,
    download_tiles_zip,
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
tab1, tab2, tab3, tab4 = st.tabs(["🏗️ GML → IFC", "📏 TIF → 3D Contour SHP", "🏔️ TIF → 3D Grid SHP", "🔄 Format Tools"])

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
                _m_prog = st.progress(0.0, text="Parsing files...")
                _m_stat = st.empty()
                with st.spinner("Merging and converting..."):
                    gml_file_list = []
                    for uf in uploaded_files:
                        gml_file_list.append((uf.name, uf.read()))

                    def _merge_cb(done, total, name):
                        _m_prog.progress(done / max(total, 1),
                                         text=f"Building {done+1}/{total} — {name}")
                        _m_stat.caption(f"▶ {name}")

                    result = convert_gml_files_merged(
                        gml_file_list=gml_file_list,
                        use_map_conversion=use_map_conversion,
                        boundary_polygon=boundary_polygon,
                        input_crs_key=input_crs_key,
                        output_crs_key=output_crs_key,
                        color_map=color_map,
                        split_by_surface=split_by_surface,
                        progress_callback=_merge_cb,
                    )
                _m_prog.progress(1.0, text="Done")
                _m_stat.empty()

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
                        _f_prog = st.progress(0.0, text="Parsing buildings...")
                        _f_stat = st.empty()

                        def _file_cb(done, total, name, _p=_f_prog, _s=_f_stat):
                            _p.progress(done / max(total, 1),
                                        text=f"Building {done+1}/{total} — {name}")
                            _s.caption(f"▶ {name}")

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
                                progress_callback=_file_cb,
                            )
                        _f_prog.progress(1.0, text="Done")
                        _f_stat.empty()

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
    tif_files_list = []   # [(name, bytes), ...] for batch mode

    # ── UPLOAD TIF BRANCH ──
    if input_mode == "Upload TIF":
        st.subheader("Upload TIF File(s)")

        uploaded_tifs = st.file_uploader(
            "Select GeoTIFF elevation file(s) or a ZIP of TIF files",
            type=['tif', 'tiff', 'TIF', 'TIFF', 'zip'],
            accept_multiple_files=True,
            help="Upload one or more GeoTIFF files, or a ZIP containing TIF files.",
            key="tif_uploader"
        )

        if uploaded_tifs:
            for _f in uploaded_tifs:
                if _f.name.lower().endswith('.zip'):
                    try:
                        with zipfile.ZipFile(io.BytesIO(_f.read())) as _zf:
                            for _n in _zf.namelist():
                                if _n.lower().endswith(('.tif', '.tiff')):
                                    tif_files_list.append((_n, _zf.read(_n)))
                    except Exception as _ze:
                        st.error(f"Cannot read ZIP {_f.name}: {_ze}")
                else:
                    tif_files_list.append((_f.name, _f.read()))

        if tif_files_list:
            tif_bytes = tif_files_list[0][1]
            source_filename = tif_files_list[0][0]
            if len(tif_files_list) > 1:
                st.info(f"**{len(tif_files_list)} files loaded.** Settings below apply to all.")
                with st.expander("Files loaded"):
                    for _n, _b in tif_files_list:
                        st.caption(f"• {_n}  ({len(_b)/1024/1024:.1f} MB)")

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
            tif_files_list = [(source_filename, tif_bytes)]

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
            _n_files = len(tif_files_list) if tif_files_list else 1
            _btn_label = "Generate Contours" if _n_files <= 1 else f"Generate Contours ({_n_files} files)"
            if st.button(_btn_label, type="primary", use_container_width=True):

                if _n_files <= 1:
                    # ── SINGLE FILE ──
                    with st.spinner("Generating contours..."):
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
                        st.info("ZIP contains all shapefile components (.shp, .shx, .dbf, .prj).")

                    else:
                        st.error(f"Failed: {result.get('error', 'Unknown error')}")

                else:
                    # ── BATCH MODE ──
                    prog = st.progress(0.0)
                    batch_results = []
                    for _i, (_fname, _fbytes) in enumerate(tif_files_list):
                        prog.progress(_i / _n_files, text=f"Processing {_fname} ({_i+1}/{_n_files})...")
                        _r = convert_tif_to_shapefile(
                            tif_bytes=_fbytes,
                            filename=_fname,
                            interval=interval,
                            min_elevation=min_elev,
                            max_elevation=max_elev,
                            simplification_method=simplification_method,
                            simplification_params=simplification_params,
                            output_crs=output_crs,
                        )
                        batch_results.append((_fname, _r))
                    prog.progress(1.0, text="Done")

                    _ok = [(_n, _r) for _n, _r in batch_results if _r['success']]
                    _fail = [(_n, _r) for _n, _r in batch_results if not _r['success']]

                    if _ok:
                        st.success(f"Generated contours for {len(_ok)}/{_n_files} files")

                        # Pack all shapefiles into one combined ZIP
                        _combined = io.BytesIO()
                        with zipfile.ZipFile(_combined, 'w', zipfile.ZIP_DEFLATED) as _outer:
                            for _fname, _r in _ok:
                                _inner = zipfile.ZipFile(io.BytesIO(_r['zip_bytes']))
                                for _item in _inner.namelist():
                                    _outer.writestr(_item, _inner.read(_item))
                        _combined.seek(0)

                        with st.expander("Results", expanded=True):
                            for _fname, _r in _ok:
                                _m = _r['metadata']
                                st.caption(
                                    f"✓ {_fname}: {_m['num_contours']:,} contours "
                                    f"@ {_m['interval']}m interval"
                                )
                            for _fname, _r in _fail:
                                st.caption(f"✗ {_fname}: {_r.get('error')}")

                        _zip_mb = len(_combined.getvalue()) / (1024 * 1024)
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.download_button(
                                label=f"Download contours_combined.zip ({len(_ok)} shapefiles)",
                                data=_combined.getvalue(),
                                file_name="contours_combined.zip",
                                mime="application/zip",
                                use_container_width=True,
                                key="download_contours_batch"
                            )
                        with col2:
                            st.metric("ZIP Size", f"{_zip_mb:.1f} MB")

                        st.info("All shapefiles are in one ZIP. Extract to the same folder before use.")

                    if _fail:
                        for _fname, _r in _fail:
                            st.error(f"✗ {_fname}: {_r.get('error', 'Unknown error')}")

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
# TAB 3: TIF TO 3D DTM POINTS
# ============================================================================
with tab3:
    st.subheader("TIF to 3D DTM Points")
    st.markdown("Generate adaptive 3D loci from DGM elevation data for Vectorworks DTM import")

    uploaded_grid_tif = st.file_uploader(
        "Upload DGM GeoTIFF (float32 single band)",
        type=['tif', 'tiff', 'TIF', 'TIFF'],
        help="Upload a real elevation GeoTIFF (e.g., DGM1 tiles from daten-hamburg.de), NOT a styled WMS image",
        key="grid_tif_uploader"
    )

    if uploaded_grid_tif:
        grid_tif_bytes = uploaded_grid_tif.read()
        file_size = len(grid_tif_bytes) / (1024 * 1024)

        st.divider()

        with st.spinner("Analyzing GeoTIFF..."):
            grid_meta = extract_tif_metadata(grid_tif_bytes)

        if grid_meta['success']:
            st.success("DGM data loaded successfully")

            with st.expander("TIF Metadata", expanded=True):
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Resolution", f"{grid_meta['resolution']:.3f} m")
                    st.metric("Width", f"{grid_meta['width']} px")
                with col2:
                    st.metric("EPSG Code", grid_meta['epsg_code'] or "Unknown")
                    st.metric("Height", f"{grid_meta['height']} px")
                with col3:
                    st.metric("File Size", f"{file_size:.1f} MB")
                    nodata_pct = grid_meta.get('nodata_pct', 0)
                    st.metric("NoData", f"{nodata_pct:.1f}%")

                st.markdown("**Elevation Range:**")
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Min", f"{grid_meta['elevation_range']['min']:.2f} m")
                with col2:
                    st.metric("Mean", f"{grid_meta['elevation_range']['mean']:.2f} m")
                with col3:
                    st.metric("Max", f"{grid_meta['elevation_range']['max']:.2f} m")

                bounds = grid_meta['bounds']
                st.caption(f"Left: {bounds['left']:.2f} | Bottom: {bounds['bottom']:.2f} | Right: {bounds['right']:.2f} | Top: {bounds['top']:.2f}")

            st.divider()

            # Optional BBox clip
            grid_clip_gdf = None
            with st.expander("Clip to Area (optional)"):
                grid_clip_shp = st.file_uploader(
                    "Upload boundary SHP (ZIP)",
                    type=['zip'],
                    help="ZIP containing a Shapefile to clip the grid extent",
                    key="grid_clip_shp"
                )
                if grid_clip_shp:
                    try:
                        import geopandas as _gpd
                        import tempfile as _tmpmod
                        import zipfile as _zipmod

                        clip_bytes = grid_clip_shp.read()
                        with _tmpmod.TemporaryDirectory() as tmpdir:
                            zip_path = Path(tmpdir) / "clip.zip"
                            zip_path.write_bytes(clip_bytes)
                            with _zipmod.ZipFile(zip_path, 'r') as zf:
                                zf.extractall(tmpdir)
                            shp_files = list(Path(tmpdir).glob("**/*.shp"))
                            if not shp_files:
                                st.error("No .shp file found in the uploaded ZIP")
                            else:
                                grid_clip_gdf = _gpd.read_file(shp_files[0])
                                st.success(f"Clip boundary loaded: {len(grid_clip_gdf)} feature(s)")
                    except Exception as e:
                        st.error(f"Error reading clip SHP: {e}")

            # Export mode
            st.markdown("**Export Mode:**")
            export_mode = st.radio(
                "Mode",
                ["Combined: Contours + Critical Points (recommended)",
                 "Adaptive point thinning",
                 "Uniform point grid"],
                index=0,
                help="Combined gives best accuracy with fewest features. Adaptive uses quadtree thinning. Uniform is a simple fixed grid.",
                key="export_mode"
            )

            if export_mode.startswith("Combined"):
                mode_key = 'combined'
                elev_span = grid_meta['elevation_range']['max'] - grid_meta['elevation_range']['min']
                suggested = suggest_contour_interval(
                    grid_meta['resolution'],
                    grid_meta['elevation_range']['min'],
                    grid_meta['elevation_range']['max'],
                    target_contours=50,
                )
                col1, col2 = st.columns(2)
                with col1:
                    contour_interval = st.number_input(
                        "Contour interval (m)",
                        min_value=0.05,
                        max_value=5.0,
                        value=min(0.25, float(suggested)),
                        step=0.05,
                        format="%.2f",
                        help=f"Elevation span: {elev_span:.1f}m. Smaller = more accurate, more polylines.",
                        key="combined_interval"
                    )
                with col2:
                    edge_step = st.select_slider(
                        "Edge point density",
                        options=[1, 2, 3, 5],
                        value=2,
                        help="Pixel step along building NoData edges. 1 = every pixel, 2 = every other.",
                        key="edge_step"
                    )
                est_contours = int(elev_span / contour_interval) if contour_interval > 0 else 0
                st.caption(f"~{est_contours} contour levels + peaks/pits + building-edge points. "
                           f"Max vertical error: {contour_interval/2:.3f}m (corrected by critical points)")
                max_error = 0.10
                coarse_step = 10
                step_val = 5

            elif export_mode.startswith("Adaptive"):
                mode_key = 'adaptive'
                col1, col2 = st.columns(2)
                with col1:
                    max_error = st.number_input(
                        "Max error (m)",
                        min_value=0.01,
                        max_value=1.0,
                        value=0.10,
                        step=0.05,
                        format="%.2f",
                        help="Maximum allowed interpolation error.",
                        key="max_error"
                    )
                with col2:
                    coarse_step = st.select_slider(
                        "Coarse step (px)",
                        options=[4, 5, 8, 10, 15, 20],
                        value=10,
                        help="Starting grid step before adaptive refinement.",
                        key="coarse_step"
                    )
                st.caption(f"Quadtree subdivision where error > {max_error}m. Dense near slopes & buildings.")
                step_val = coarse_step
                contour_interval = 0.25
                edge_step = 2

            else:
                mode_key = 'uniform'
                step_val = st.select_slider(
                    "Grid Step (pixels)",
                    options=[1, 2, 3, 5, 10, 20],
                    value=5,
                    help="Cell size in pixels. 5 = every 5th pixel (5m at 1m DGM).",
                    key="grid_step"
                )
                max_error = 0.10
                coarse_step = 10
                contour_interval = 0.25
                edge_step = 2
                est_pts = (grid_meta['width'] // step_val) * (grid_meta['height'] // step_val)
                cell_size = grid_meta['resolution'] * step_val
                st.caption(f"~{est_pts:,} points ({cell_size:.0f}m spacing)")
                if est_pts > 100000:
                    st.warning("Consider a larger step for better VW performance.")

            # Output CRS
            st.markdown("**Output CRS:**")
            _grid_crs_labels = [f"{code} — {name}" for code, name in CRS_OPTIONS.items()]
            grid_crs_label = st.selectbox(
                "Output coordinate system",
                ["Same as input"] + _grid_crs_labels,
                index=0,
                help=f"Input CRS: EPSG:{grid_meta['epsg_code'] or '?'}",
                key="grid_output_crs"
            )
            if grid_crs_label == "Same as input":
                grid_output_crs = None
            else:
                grid_output_crs = resolve_crs(grid_crs_label.split(" — ")[0])

            st.divider()

            # Generate
            btn_label = "Generate DTM Data" if mode_key == 'combined' else "Generate 3D Points"
            if st.button(btn_label, type="primary", use_container_width=True, key="grid_generate"):
                if mode_key == 'combined':
                    with st.spinner("Generating contours + critical points..."):
                        grid_result = convert_tif_to_combined_dtm(
                            tif_bytes=grid_tif_bytes,
                            filename=uploaded_grid_tif.name,
                            contour_interval=contour_interval,
                            clip_gdf=grid_clip_gdf,
                            output_crs=grid_output_crs,
                            edge_step=edge_step,
                        )
                else:
                    with st.spinner("Generating 3D DTM points..."):
                        grid_result = convert_tif_to_dtm_points(
                            tif_bytes=grid_tif_bytes,
                            filename=uploaded_grid_tif.name,
                            mode=mode_key,
                            step=step_val,
                            max_error=max_error,
                            coarse_step=coarse_step,
                            clip_gdf=grid_clip_gdf,
                            output_crs=grid_output_crs,
                        )

                if grid_result['success']:
                    meta = grid_result['metadata']

                    if mode_key == 'combined':
                        total_features = meta['num_contours'] + meta['num_critical_points']
                        st.success(f"Generated {meta['num_contours']:,} contour polylines + {meta['num_critical_points']:,} critical points!")

                        with st.expander("Generation Details", expanded=True):
                            col1, col2, col3 = st.columns(3)
                            with col1:
                                st.metric("Contour Lines", f"{meta['num_contours']:,}")
                                st.metric("Interval", f"{meta['contour_interval']}m")
                            with col2:
                                st.metric("Critical Points", f"{meta['num_critical_points']:,}")
                                st.caption(f"Peaks: {meta['num_peaks']} | Pits: {meta['num_pits']} | Edges: {meta['num_edges']}")
                            with col3:
                                out_crs = meta.get('output_crs_name')
                                src_epsg = meta['epsg_code']
                                if out_crs and out_crs != str(src_epsg):
                                    st.metric("Output CRS", out_crs)
                                    st.caption(f"(from EPSG:{src_epsg})")
                                else:
                                    st.metric("EPSG Code", src_epsg or "Unknown")

                            total_valid = grid_meta['width'] * grid_meta['height'] * (1 - grid_meta.get('nodata_pct', 0) / 100)
                            st.caption(f"Total features: {total_features:,} from {int(total_valid):,} valid pixels ({total_valid/max(1,total_features):.0f}x reduction)")

                    else:
                        st.success(f"Generated {meta['num_points']:,} 3D points!")

                        with st.expander("Generation Details", expanded=True):
                            col1, col2, col3 = st.columns(3)
                            with col1:
                                st.metric("Points", f"{meta['num_points']:,}")
                                if meta['mode'] == 'adaptive':
                                    st.metric("Max Error", f"{meta['max_error']}m")
                                else:
                                    st.metric("Grid Step", f"{meta['grid_step']} px")
                            with col2:
                                out_crs = meta.get('output_crs_name')
                                src_epsg = meta['epsg_code']
                                if out_crs and out_crs != str(src_epsg):
                                    st.metric("Output CRS", out_crs)
                                    st.caption(f"(from EPSG:{src_epsg})")
                                else:
                                    st.metric("EPSG Code", src_epsg or "Unknown")
                            with col3:
                                st.metric("Mode", meta['mode'].title())

                            total_valid = grid_meta['width'] * grid_meta['height'] * (1 - grid_meta.get('nodata_pct', 0) / 100)
                            ratio = total_valid / meta['num_points'] if meta['num_points'] > 0 else 0
                            st.caption(f"{meta['num_points']:,} points from {int(total_valid):,} pixels ({ratio:.0f}x reduction)")

                    st.divider()

                    zip_size_kb = len(grid_result['zip_bytes']) / 1024
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.download_button(
                            label=f"Download {grid_result['filename']}",
                            data=grid_result['zip_bytes'],
                            file_name=grid_result['filename'],
                            mime="application/zip",
                            use_container_width=True,
                            key="download_grid_shp"
                        )
                    with col2:
                        if zip_size_kb > 1024:
                            st.metric("Size", f"{zip_size_kb/1024:.1f} MB")
                        else:
                            st.metric("Size", f"{zip_size_kb:.1f} KB")

                    if mode_key == 'combined':
                        st.info("ZIP contains 2 shapefiles: **contours** (PolylineZ) + **critical points** (PointZ). "
                                "Import both into Vectorworks, select all, then **Create DTM from Source Data**.")
                    else:
                        st.info("Import the PointZ shapefile into Vectorworks as 3D Loci, then use **Create DTM from Source Data**.")

                else:
                    st.error(f"Failed: {grid_result.get('error', 'Unknown error')}")

        else:
            st.error(f"Failed to read TIF: {grid_meta.get('error', 'Unknown error')}")

    else:
        st.info("Upload a DGM GeoTIFF (float32 elevation data) to get started")

        with st.expander("About this converter"):
            st.markdown("""
            Generate optimized DTM source data from DGM elevation rasters
            for import into Vectorworks.

            **Important:** Use real elevation GeoTIFFs (float32), NOT styled WMS images.
            Download DGM1 tiles from daten-hamburg.de or similar open data portals.

            **Export Modes:**
            - **Combined** (recommended): Contour polylines + critical points (peaks,
              pits, building-edge ground truth). Best accuracy-to-size ratio. Two
              shapefiles in one ZIP — import both into VW for DTM generation.
            - **Adaptive**: Quadtree point thinning. Dense near slopes & NoData edges,
              sparse in flat areas.
            - **Uniform**: Fixed grid step everywhere. Simple but less efficient.

            **Output:**
            - Combined: PolylineZ contours + PointZ critical points in one ZIP
            - Adaptive/Uniform: PointZ shapefile
            - CRS reprojection (including LS320 for Hamburg)
            - Import into VW → Create DTM from Source Data
            """)


# ============================================================================
# TAB 4: FORMAT TOOLS  (XYZ->TIF | XML->GML | DGM1 Tile Downloader)
# ============================================================================
with tab4:
    st.subheader("Format Tools")
    st.markdown("Pre-process raw data files before using them in other tabs")

    tool_choice = st.radio(
        "Tool",
        ["📐 XYZ → GeoTIFF", "📄 XML → GML",
         "🗺️ XYZ Tile Downloader", "🏙️ GML/XML Tile Downloader"],
        horizontal=True,
        key="format_tool_choice",
    )

    st.divider()

    # -- XYZ -> GeoTIFF -------------------------------------------------------
    if tool_choice == "📐 XYZ → GeoTIFF":
        st.subheader("XYZ Point Cloud → GeoTIFF")
        st.markdown(
            "Convert space-separated **X Y Z** point cloud tiles to float32 GeoTIFF. "
            "Upload a single **.xyz** file or a **ZIP** of multiple .xyz files."
        )

        uploaded_xyz = st.file_uploader(
            "Upload .xyz file or ZIP of .xyz files",
            type=["xyz", "XYZ", "txt", "zip"],
            help="Single XYZ file or ZIP from the DGM1 Tile Downloader.",
            key="xyz_uploader",
        )

        if uploaded_xyz:
            is_zip = uploaded_xyz.name.lower().endswith(".zip")
            xyz_bytes = uploaded_xyz.read()
            file_size_mb = len(xyz_bytes) / (1024 * 1024)

            st.divider()
            st.markdown("**Coordinate System:**")

            _default_epsg = 25832
            if "_33_" in uploaded_xyz.name or "_33." in uploaded_xyz.name:
                _default_epsg = 25833

            xyz_epsg = st.number_input(
                "EPSG Code",
                min_value=1024,
                max_value=99999,
                value=_default_epsg,
                step=1,
                help="DGM tiles: 25832 (UTM 32N) or 25833 (UTM 33N).",
                key="xyz_epsg",
            )

            xyz_nodata = st.number_input(
                "NoData value for empty pixels",
                value=-9999.0,
                step=1.0,
                format="%.1f",
                key="xyz_nodata",
            )

            st.divider()

            if is_zip:
                import zipfile as _zf_check
                try:
                    _names = [n for n in _zf_check.ZipFile(io.BytesIO(xyz_bytes)).namelist()
                              if n.lower().endswith(".xyz")]
                    st.info(f"ZIP contains **{len(_names)}** XYZ file(s): {', '.join(_names)}")
                except Exception as _e:
                    st.error(f"Cannot read ZIP: {_e}")
                    _names = []

                if _names and st.button("Convert All to GeoTIFF (ZIP output)", type="primary",
                                        use_container_width=True, key="xyz_batch_convert"):
                    prog = st.progress(0.0, text="Starting batch conversion...")
                    result = convert_xyz_zip_to_tif_zip(
                        zip_bytes=xyz_bytes,
                        epsg_code=int(xyz_epsg),
                        nodata=float(xyz_nodata),
                    )
                    prog.progress(1.0, text="Done")

                    if result["success"]:
                        zip_size_mb = len(result["zip_bytes"]) / (1024 * 1024)
                        st.success(f"Converted {result['num_success']}/{result['num_total']} files")

                        with st.expander("Results", expanded=True):
                            for r in result["results"]:
                                if r["success"]:
                                    m = r["metadata"]
                                    st.caption(
                                        f"✓ {r['output']}  —  "
                                        f"{m['width']}×{m['height']} px, "
                                        f"{m['resolution']:.1f}m res, "
                                        f"Z {m['elevation_range']['min']:.1f}–{m['elevation_range']['max']:.1f} m"
                                    )
                                else:
                                    st.caption(f"✗ {r['input']}: {r.get('error')}")

                        out_name = Path(uploaded_xyz.name).stem + "_tif.zip"
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.download_button(
                                label=f"Download {out_name} ({result['num_success']} GeoTIFFs)",
                                data=result["zip_bytes"],
                                file_name=out_name,
                                mime="application/zip",
                                use_container_width=True,
                                key="xyz_batch_download",
                            )
                        with col2:
                            st.metric("ZIP Size", f"{zip_size_mb:.1f} MB")

                        st.info("Upload individual TIF files in the TIF converter tabs.")
                    else:
                        st.error(f"Batch conversion failed: {result.get('error')}")

            else:
                with st.spinner("Analyzing point cloud..."):
                    info = analyze_xyz(xyz_bytes)

                if info["success"]:
                    st.success(f"Loaded {info['num_points']:,} points")

                    with st.expander("Point Cloud Info", expanded=True):
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.metric("Points", f"{info['num_points']:,}")
                            st.metric("Resolution", f"{info['resolution']:.3f} m")
                        with col2:
                            st.metric("Grid Width", f"{info['width']} px")
                            st.metric("Grid Height", f"{info['height']} px")
                        with col3:
                            st.metric("File Size", f"{file_size_mb:.1f} MB")
                        st.markdown("**Elevation Range:**")
                        c1, c2, c3 = st.columns(3)
                        with c1:
                            st.metric("Min Z", f"{info['elevation_range']['min']:.2f} m")
                        with c2:
                            st.metric("Mean Z", f"{info['elevation_range']['mean']:.2f} m")
                        with c3:
                            st.metric("Max Z", f"{info['elevation_range']['max']:.2f} m")
                        st.caption(
                            f"X: {info['x_min']:.2f} → {info['x_max']:.2f}  |  "
                            f"Y: {info['y_min']:.2f} → {info['y_max']:.2f}"
                        )

                    if st.button("Convert to GeoTIFF", type="primary", use_container_width=True, key="xyz_convert"):
                        with st.spinner(f"Rasterizing {info['num_points']:,} points..."):
                            result = convert_xyz_to_tif(
                                xyz_bytes=xyz_bytes,
                                epsg_code=int(xyz_epsg),
                                nodata=float(xyz_nodata),
                            )
                        if result["success"]:
                            meta = result["metadata"]
                            tif_size_mb = len(result["tif_bytes"]) / (1024 * 1024)
                            st.success(
                                f"GeoTIFF created — {meta['width']}×{meta['height']} px, "
                                f"EPSG:{meta['epsg_code']}, {tif_size_mb:.1f} MB"
                            )
                            out_name = Path(uploaded_xyz.name).stem + ".tif"
                            col1, col2 = st.columns([3, 1])
                            with col1:
                                st.download_button(
                                    label=f"Download {out_name}",
                                    data=result["tif_bytes"],
                                    file_name=out_name,
                                    mime="image/tiff",
                                    use_container_width=True,
                                    key="xyz_download",
                                )
                            with col2:
                                st.metric("Size", f"{tif_size_mb:.1f} MB")
                            st.info("Upload this TIF in the TIF converter tabs.")
                        else:
                            st.error(f"Conversion failed: {result.get('error')}")
                else:
                    st.error(f"Failed to read XYZ file: {info.get('error')}")

        else:
            st.info("Upload an .xyz file or a ZIP of .xyz files to get started")
            with st.expander("About XYZ → GeoTIFF"):
                st.markdown("""
                Converts regular-grid XYZ point clouds to float32 GeoTIFF.

                **Inputs:**
                - Single `.xyz`: space-separated X Y Z, one point per line
                - ZIP of `.xyz` files: batch-converts all, outputs a ZIP of TIFs

                Auto-detects resolution from point spacing and EPSG from filename.
                Output: LZW-compressed float32 GeoTIFF.
                """)

    # -- XML -> GML -----------------------------------------------------------
    elif tool_choice == "📄 XML → GML":
        st.subheader("XML → GML (CityGML rename)")
        st.markdown(
            "CityGML building data is sometimes delivered with an `.xml` extension. "
            "This tool validates the file and provides it as `.gml` — "
            "**content is byte-identical to the source**."
        )

        uploaded_xml = st.file_uploader(
            "Upload .xml file(s) or a ZIP of .xml files",
            type=["xml", "XML", "zip"],
            accept_multiple_files=True,
            help="Single or multiple XML files, or a ZIP containing .xml files.",
            key="xml_uploader",
        )

        if uploaded_xml:
            # Collect (name, bytes) pairs from both direct uploads and ZIPs
            xml_files = []
            for _f in uploaded_xml:
                if _f.name.lower().endswith(".zip"):
                    try:
                        with zipfile.ZipFile(io.BytesIO(_f.getvalue())) as _zf:
                            for _n in _zf.namelist():
                                if _n.lower().endswith(".xml"):
                                    xml_files.append((_n, _zf.read(_n)))
                    except Exception as _ze:
                        st.error(f"Cannot read ZIP {_f.name}: {_ze}")
                else:
                    xml_files.append((_f.name, _f.getvalue()))

            if xml_files:
                if len(xml_files) == 1:
                    # ── SINGLE FILE ──
                    _name, _bytes = xml_files[0]
                    file_size_kb = len(_bytes) / 1024
                    with st.spinner("Validating XML..."):
                        result = convert_xml_to_gml(_bytes)
                    if result["success"]:
                        st.success("Valid XML — ready for download as .gml")
                        with st.expander("File Info", expanded=True):
                            col1, col2, col3 = st.columns(3)
                            with col1:
                                st.metric("File Size", f"{file_size_kb:.1f} KB")
                            with col2:
                                st.metric("City Objects", result["num_members"])
                            with col3:
                                st.metric("CityGML", "Yes" if result["is_citygml"] else "No")
                            st.caption(f"Root element: {result['root_tag']}")
                        out_name = Path(_name).stem + ".gml"
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.download_button(
                                label=f"Download {out_name}",
                                data=result["gml_bytes"],
                                file_name=out_name,
                                mime="application/gml+xml",
                                use_container_width=True,
                                key="xml_download",
                            )
                        with col2:
                            st.metric("Size", f"{file_size_kb:.1f} KB")
                        st.info(f"`{out_name}` is byte-identical to the source. Upload in **GML → IFC** tab.")
                    else:
                        st.error(f"Validation failed: {result.get('error')}")

                else:
                    # ── BATCH MODE ──
                    st.info(f"**{len(xml_files)} XML files** loaded. Validating and renaming all to .gml.")
                    with st.spinner(f"Processing {len(xml_files)} files..."):
                        _ok = []
                        _fail = []
                        _zip_buf = io.BytesIO()
                        with zipfile.ZipFile(_zip_buf, "w", zipfile.ZIP_DEFLATED) as _zout:
                            for _name, _bytes in xml_files:
                                _r = convert_xml_to_gml(_bytes)
                                _gml_name = Path(_name).stem + ".gml"
                                if _r["success"]:
                                    _zout.writestr(_gml_name, _r["gml_bytes"])
                                    _ok.append({"name": _gml_name, "members": _r["num_members"],
                                                "size_kb": len(_bytes) / 1024})
                                else:
                                    _fail.append({"name": _name, "error": _r.get("error")})

                    if _ok:
                        st.success(f"Converted {len(_ok)}/{len(xml_files)} files")
                        with st.expander("Results", expanded=True):
                            for _d in _ok:
                                st.caption(f"✓ {_d['name']}  ({_d['members']} city objects, {_d['size_kb']:.0f} KB)")
                            for _d in _fail:
                                st.caption(f"✗ {_d['name']}: {_d['error']}")

                        _zip_buf.seek(0)
                        _zip_mb = len(_zip_buf.getvalue()) / (1024 * 1024)
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.download_button(
                                label=f"Download gml_files.zip ({len(_ok)} .gml files)",
                                data=_zip_buf.getvalue(),
                                file_name="gml_files.zip",
                                mime="application/zip",
                                use_container_width=True,
                                key="xml_batch_download",
                            )
                        with col2:
                            st.metric("ZIP Size", f"{_zip_mb:.1f} MB")
                        st.info("All .gml files are byte-identical to their sources. Upload in **GML → IFC** tab.")
                    for _d in _fail:
                        st.error(f"✗ {_d['name']}: {_d['error']}")

        else:
            st.info("Upload a .xml file, multiple .xml files, or a ZIP of .xml files to get started")
            with st.expander("About XML → GML"):
                st.markdown("""
                CityGML files from some open geodata portals are distributed with `.xml`
                instead of `.gml`. Both are identical. This tool validates and renames.
                No data is modified. Accepts single file, multiple files, or a ZIP.
                **After conversion:** Upload the `.gml` file(s) in the **GML → IFC** tab.
                """)

    # -- XYZ TILE DOWNLOADER --------------------------------------------------
    elif tool_choice == "🗺️ XYZ Tile Downloader":
        st.subheader("XYZ Tile Downloader")
        st.markdown(
            "Upload a GeoJSON tile index, pick tiles on the interactive map, "
            "then download them as a ZIP of XYZ files ready for the **XYZ → GeoTIFF** tool."
        )

        uploaded_geojson = st.file_uploader(
            "Upload GeoJSON tile index",
            type=["geojson", "json"],
            help="GeoJSON file from your geodata portal containing tile polygons with download links.",
            key="dgm1_geojson",
        )

        if uploaded_geojson:
            geojson_bytes = uploaded_geojson.getvalue()
            _xyz_file_id = (uploaded_geojson.name, len(geojson_bytes))
            if st.session_state.get("_xyz_tile_file_id") != _xyz_file_id:
                with st.spinner("Parsing tile index..."):
                    st.session_state["_xyz_tile_file_id"] = _xyz_file_id
                    st.session_state["_xyz_tile_index"] = parse_tile_index(geojson_bytes)
            tile_index = st.session_state["_xyz_tile_index"]
            st.success(f"Tile index loaded — {len(tile_index):,} tiles")

            # session state init
            if "dgm1_selected" not in st.session_state:
                st.session_state.dgm1_selected = set()
            if "dgm1_center" not in st.session_state:
                st.session_state.dgm1_center = ""
            if "dgm1_map_center" not in st.session_state:
                st.session_state.dgm1_map_center = None
            if "dgm1_map_zoom" not in st.session_state:
                st.session_state.dgm1_map_zoom = 13
            if "dgm1_last_click" not in st.session_state:
                st.session_state.dgm1_last_click = None

            st.divider()

            col_c, col_btn, col_clr = st.columns([2, 1, 1])
            with col_c:
                center_input = st.text_input(
                    "Center tile (kachel ID)",
                    value=st.session_state.dgm1_center,
                    key="dgm1_center_input",
                    help="9-digit ID: zone(2) + easting_km(3) + northing_km(4), e.g. 326226017",
                )
                if center_input != st.session_state.dgm1_center:
                    st.session_state.dgm1_center = center_input
                    st.session_state.dgm1_map_center = None
                    st.rerun()

            center_kachel = st.session_state.dgm1_center

            with col_btn:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("Select 3×3 grid", use_container_width=True, key="dgm1_auto3x3"):
                    st.session_state.dgm1_selected = grid_kachels(center_kachel, tile_index, radius=1)
                    st.rerun()

            with col_clr:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("Clear", use_container_width=True, key="dgm1_clear"):
                    st.session_state.dgm1_selected = set()
                    st.rerun()

            if not center_kachel:
                st.info("Enter a 9-digit kachel ID above to load the map.")
            elif center_kachel not in tile_index:
                st.warning(f"Tile `{center_kachel}` not found in index.")
            else:
                if st.session_state.dgm1_map_center is None:
                    lat0, lon0 = kachel_center_latlon(center_kachel)
                    st.session_state.dgm1_map_center = [lat0, lon0]

                selected = st.session_state.dgm1_selected

                view_radius = st.slider(
                    "Map view radius (tiles)", 5, 30, 12, 1,
                    help="How many km around center to render",
                    key="dgm1_view_radius",
                )

                view_gj = build_view_geojson(tile_index, selected, center_kachel, radius=view_radius)

                def _tile_style(feature):
                    k = feature["properties"]["kachel"]
                    if k in selected:
                        return {"fillColor": "#1565C0", "color": "#0D47A1",
                                "weight": 2, "fillOpacity": 0.65}
                    elif feature["properties"]["is_center"]:
                        return {"fillColor": "#F9A825", "color": "#F57F17",
                                "weight": 2, "fillOpacity": 0.55}
                    else:
                        return {"fillColor": "#90CAF9", "color": "#64B5F6",
                                "weight": 1, "fillOpacity": 0.20}

                m = folium.Map(
                    location=st.session_state.dgm1_map_center,
                    zoom_start=st.session_state.dgm1_map_zoom,
                    tiles="CartoDB positron",
                )

                folium.GeoJson(
                    view_gj,
                    style_function=_tile_style,
                    tooltip=folium.GeoJsonTooltip(
                        fields=["kachel", "datum"],
                        aliases=["Tile ID", "Date"],
                        style="font-size:12px;",
                        sticky=True,
                    ),
                ).add_to(m)

                st.caption("🖱️ Click a tile to select/deselect. Hover for tile info.")
                map_out = st_folium(
                    m,
                    width="100%",
                    height=480,
                    key="dgm1_map",
                    returned_objects=["last_clicked", "center", "zoom"],
                )

                if map_out:
                    if map_out.get("center"):
                        st.session_state.dgm1_map_center = [
                            map_out["center"]["lat"],
                            map_out["center"]["lng"],
                        ]
                    if map_out.get("zoom"):
                        st.session_state.dgm1_map_zoom = map_out["zoom"]

                    lc = map_out.get("last_clicked")
                    if lc:
                        click_key = (round(lc["lat"], 7), round(lc["lng"], 7))
                        if click_key != st.session_state.dgm1_last_click:
                            st.session_state.dgm1_last_click = click_key
                            hit = kachel_from_latlon(lc["lat"], lc["lng"], tile_index)
                            if hit:
                                if hit in st.session_state.dgm1_selected:
                                    st.session_state.dgm1_selected.discard(hit)
                                else:
                                    st.session_state.dgm1_selected.add(hit)
                                st.rerun()

                st.divider()

                # tic-tac-toe 3x3 grid
                st.markdown("**3×3 grid around center** — click to toggle:")
                cells = get_grid_cells(center_kachel, tile_index, radius=1)
                grid_cols = st.columns(3)
                for i, cell in enumerate(cells):
                    k = cell["kachel"]
                    is_sel = k in selected
                    is_ctr = k == center_kachel
                    with grid_cols[i % 3]:
                        if cell["available"]:
                            prefix = ("★ " if is_ctr else "") + ("✓ " if is_sel else "")
                            label = f"{prefix}{k}\n{cell['datum']}"
                            btype = "primary" if is_sel else "secondary"
                            if st.button(label, key=f"cell_{k}", use_container_width=True, type=btype):
                                if is_sel:
                                    st.session_state.dgm1_selected.discard(k)
                                else:
                                    st.session_state.dgm1_selected.add(k)
                                st.rerun()
                        else:
                            st.button(f"❌ {k}\nn/a", key=f"cell_{k}",
                                      disabled=True, use_container_width=True)

                st.divider()

                n_sel = len(selected)
                if n_sel == 0:
                    st.info("No tiles selected. Click tiles on the map or use the grid buttons above.")
                else:
                    st.subheader(f"📥 {n_sel} tile(s) selected")
                    with st.expander("Selected tiles", expanded=n_sel <= 12):
                        for k in sorted(selected):
                            info = tile_index.get(k, {})
                            st.caption(f"• **{k}** — {info.get('datum','?')}")

                    est_mb = n_sel * 27
                    zip_est_mb = n_sel * 6
                    st.caption(f"Estimated: ~{est_mb} MB raw, ~{zip_est_mb} MB zipped")

                    if st.button(
                        f"⬇️ Download {n_sel} XYZ file(s) as ZIP",
                        type="primary",
                        use_container_width=True,
                        key="dgm1_download_btn",
                    ):
                        kachel_list = sorted(selected)
                        prog_bar = st.progress(0.0)
                        status_ph = st.empty()
                        zip_buf = io.BytesIO()
                        downloaded_list = []
                        failed_list = []

                        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
                            for i, kachel in enumerate(kachel_list):
                                prog_bar.progress(
                                    i / n_sel,
                                    text=f"Downloading {kachel} ({i+1}/{n_sel})...",
                                )
                                status_ph.text(f"Fetching {kachel}...")
                                info = tile_index[kachel]
                                url = info["link_data"]
                                fname = (url.split("file=")[1].split("&")[0]
                                         if "file=" in url else f"dgm1_{kachel}.xyz")
                                try:
                                    import requests as _req
                                    r = _req.get(url, timeout=180)
                                    r.raise_for_status()
                                    zf_out.writestr(fname, r.content)
                                    downloaded_list.append({
                                        "kachel": kachel,
                                        "filename": fname,
                                        "size_mb": len(r.content) / 1024 / 1024,
                                    })
                                except Exception as e:
                                    failed_list.append({"kachel": kachel, "error": str(e)})

                        prog_bar.progress(1.0, text="Complete")
                        zip_buf.seek(0)
                        zip_bytes_out = zip_buf.getvalue()

                        if downloaded_list:
                            total_raw = sum(d["size_mb"] for d in downloaded_list)
                            zip_mb = len(zip_bytes_out) / (1024 * 1024)
                            status_ph.success(
                                f"Downloaded {len(downloaded_list)} file(s) — "
                                f"{total_raw:.0f} MB raw → {zip_mb:.1f} MB ZIP"
                            )
                            st.download_button(
                                label=f"Save dgm1_tiles.zip ({len(downloaded_list)} files, {zip_mb:.1f} MB)",
                                data=zip_bytes_out,
                                file_name="dgm1_tiles.zip",
                                mime="application/zip",
                                use_container_width=True,
                                key="dgm1_zip_dl",
                            )
                            with st.expander("Download details"):
                                for d in downloaded_list:
                                    st.caption(f"✓ {d['filename']}  ({d['size_mb']:.1f} MB)")
                                for f in failed_list:
                                    st.caption(f"✗ {f['kachel']}: {f['error']}")
                            st.info(
                                "Upload **dgm1_tiles.zip** in the **📐 XYZ → GeoTIFF** tool "
                                "to batch-convert all tiles to GeoTIFF."
                            )
                        else:
                            status_ph.error("All downloads failed!")
                            for f in failed_list:
                                st.error(f"✗ {f['kachel']}: {f['error']}")

        else:
            st.info("Upload the GeoJSON tile index to get started")
            with st.expander("About XYZ Tile Downloader"):
                st.markdown("""
                Download XYZ elevation tiles from a geodata portal.

                **Steps:**
                1. Download the GeoJSON tile index from your geodata portal
                2. Upload it here
                3. Enter a center tile ID (9-digit kachel)
                4. Click **Select 3×3 grid** or click tiles individually on the map
                5. Click **Download XYZ files as ZIP**
                6. Go to **XYZ → GeoTIFF** and upload the ZIP to batch-convert

                **Tile ID format:** `ZZXXXYYY` (zone 2 + easting km 3 + northing km 4)
                """)

    # -- GML/XML TILE DOWNLOADER ----------------------------------------------
    else:
        st.subheader("GML/XML Tile Downloader")
        st.markdown(
            "Upload a GeoJSON tile index, pick tiles on the interactive map, "
            "then download them as a ZIP of GML/XML files ready for the **XML → GML** or **GML → IFC** tools."
        )

        uploaded_geojson_gml = st.file_uploader(
            "Upload GeoJSON tile index",
            type=["geojson", "json"],
            help="GeoJSON file from your geodata portal containing tile polygons with download links.",
            key="gml_tile_geojson",
        )

        if uploaded_geojson_gml:
            geojson_bytes_gml = uploaded_geojson_gml.getvalue()
            _gml_file_id = (uploaded_geojson_gml.name, len(geojson_bytes_gml))
            if st.session_state.get("_gml_tile_file_id") != _gml_file_id:
                with st.spinner("Parsing tile index..."):
                    st.session_state["_gml_tile_file_id"] = _gml_file_id
                    st.session_state["_gml_tile_index"] = parse_tile_index(geojson_bytes_gml)
            tile_index_gml = st.session_state["_gml_tile_index"]
            st.success(f"Tile index loaded — {len(tile_index_gml):,} tiles")

            if "gml_tile_selected" not in st.session_state:
                st.session_state.gml_tile_selected = set()
            if "gml_tile_center" not in st.session_state:
                st.session_state.gml_tile_center = ""
            if "gml_tile_map_center" not in st.session_state:
                st.session_state.gml_tile_map_center = None
            if "gml_tile_map_zoom" not in st.session_state:
                st.session_state.gml_tile_map_zoom = 13
            if "gml_tile_last_click" not in st.session_state:
                st.session_state.gml_tile_last_click = None

            st.divider()

            col_c, col_btn, col_clr = st.columns([2, 1, 1])
            with col_c:
                gml_center_input = st.text_input(
                    "Center tile (kachel ID)",
                    value=st.session_state.gml_tile_center,
                    key="gml_tile_center_input",
                    help="9-digit ID: zone(2) + easting_km(3) + northing_km(4)",
                )
                if gml_center_input != st.session_state.gml_tile_center:
                    st.session_state.gml_tile_center = gml_center_input
                    st.session_state.gml_tile_map_center = None
                    st.rerun()

            gml_center = st.session_state.gml_tile_center

            with col_btn:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("Select 3×3 grid", use_container_width=True, key="gml_tile_auto3x3"):
                    st.session_state.gml_tile_selected = grid_kachels(gml_center, tile_index_gml, radius=1)
                    st.rerun()

            with col_clr:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("Clear", use_container_width=True, key="gml_tile_clear"):
                    st.session_state.gml_tile_selected = set()
                    st.rerun()

            if not gml_center:
                st.info("Enter a 9-digit kachel ID above to load the map.")
            elif gml_center not in tile_index_gml:
                st.warning(f"Tile `{gml_center}` not found in index.")
            else:
                if st.session_state.gml_tile_map_center is None:
                    _lat0, _lon0 = kachel_center_latlon(gml_center)
                    st.session_state.gml_tile_map_center = [_lat0, _lon0]

                gml_selected = st.session_state.gml_tile_selected

                gml_view_radius = st.slider(
                    "Map view radius (tiles)", 5, 30, 12, 1,
                    key="gml_tile_view_radius",
                )

                gml_view_gj = build_view_geojson(
                    tile_index_gml, gml_selected, gml_center, radius=gml_view_radius
                )

                def _gml_tile_style(feature):
                    k = feature["properties"]["kachel"]
                    if k in gml_selected:
                        return {"fillColor": "#2E7D32", "color": "#1B5E20",
                                "weight": 2, "fillOpacity": 0.65}
                    elif feature["properties"]["is_center"]:
                        return {"fillColor": "#F9A825", "color": "#F57F17",
                                "weight": 2, "fillOpacity": 0.55}
                    else:
                        return {"fillColor": "#A5D6A7", "color": "#66BB6A",
                                "weight": 1, "fillOpacity": 0.20}

                gml_m = folium.Map(
                    location=st.session_state.gml_tile_map_center,
                    zoom_start=st.session_state.gml_tile_map_zoom,
                    tiles="CartoDB positron",
                )
                folium.GeoJson(
                    gml_view_gj,
                    style_function=_gml_tile_style,
                    tooltip=folium.GeoJsonTooltip(
                        fields=["kachel", "datum"],
                        aliases=["Tile ID", "Date"],
                        style="font-size:12px;",
                        sticky=True,
                    ),
                ).add_to(gml_m)

                st.caption("🖱️ Click a tile to select/deselect. Hover for tile info.")
                gml_map_out = st_folium(
                    gml_m,
                    width="100%",
                    height=480,
                    key="gml_tile_map",
                    returned_objects=["last_clicked", "center", "zoom"],
                )

                if gml_map_out:
                    if gml_map_out.get("center"):
                        st.session_state.gml_tile_map_center = [
                            gml_map_out["center"]["lat"],
                            gml_map_out["center"]["lng"],
                        ]
                    if gml_map_out.get("zoom"):
                        st.session_state.gml_tile_map_zoom = gml_map_out["zoom"]
                    lc = gml_map_out.get("last_clicked")
                    if lc:
                        ck = (round(lc["lat"], 7), round(lc["lng"], 7))
                        if ck != st.session_state.gml_tile_last_click:
                            st.session_state.gml_tile_last_click = ck
                            hit = kachel_from_latlon(lc["lat"], lc["lng"], tile_index_gml)
                            if hit:
                                if hit in gml_selected:
                                    st.session_state.gml_tile_selected.discard(hit)
                                else:
                                    st.session_state.gml_tile_selected.add(hit)
                                st.rerun()

                st.divider()

                st.markdown("**3×3 grid around center** — click to toggle:")
                gml_cells = get_grid_cells(gml_center, tile_index_gml, radius=1)
                gml_gcols = st.columns(3)
                for _i, _cell in enumerate(gml_cells):
                    _k = _cell["kachel"]
                    _is_sel = _k in gml_selected
                    _is_ctr = _k == gml_center
                    with gml_gcols[_i % 3]:
                        if _cell["available"]:
                            _pfx = ("★ " if _is_ctr else "") + ("✓ " if _is_sel else "")
                            _lbl = f"{_pfx}{_k}\n{_cell['datum']}"
                            if st.button(_lbl, key=f"gml_cell_{_k}", use_container_width=True,
                                         type="primary" if _is_sel else "secondary"):
                                if _is_sel:
                                    st.session_state.gml_tile_selected.discard(_k)
                                else:
                                    st.session_state.gml_tile_selected.add(_k)
                                st.rerun()
                        else:
                            st.button(f"❌ {_k}\nn/a", key=f"gml_cell_{_k}",
                                      disabled=True, use_container_width=True)

                st.divider()

                gml_n_sel = len(gml_selected)
                if gml_n_sel == 0:
                    st.info("No tiles selected. Click tiles on the map or use the grid buttons.")
                else:
                    st.subheader(f"📥 {gml_n_sel} tile(s) selected")
                    with st.expander("Selected tiles", expanded=gml_n_sel <= 12):
                        for _k in sorted(gml_selected):
                            _inf = tile_index_gml.get(_k, {})
                            st.caption(f"• **{_k}** — {_inf.get('datum','?')}")

                    if st.button(
                        f"⬇️ Download {gml_n_sel} file(s) as ZIP",
                        type="primary",
                        use_container_width=True,
                        key="gml_tile_dl_btn",
                    ):
                        _klist = sorted(gml_selected)
                        _prog = st.progress(0.0)
                        _status = st.empty()
                        _zbuf = io.BytesIO()
                        _dl_ok = []
                        _dl_fail = []

                        with zipfile.ZipFile(_zbuf, "w", zipfile.ZIP_DEFLATED) as _zout:
                            for _i, _kachel in enumerate(_klist):
                                _prog.progress(_i / gml_n_sel,
                                               text=f"Downloading {_kachel} ({_i+1}/{gml_n_sel})...")
                                _info = tile_index_gml[_kachel]
                                _url = _info["link_data"]
                                _fname = (_url.split("file=")[1].split("&")[0]
                                          if "file=" in _url else f"{_kachel}.gml")
                                try:
                                    import requests as _req
                                    _r = _req.get(_url, timeout=180)
                                    _r.raise_for_status()
                                    _zout.writestr(_fname, _r.content)
                                    _dl_ok.append({"kachel": _kachel, "filename": _fname,
                                                   "size_kb": len(_r.content) / 1024})
                                except Exception as _e:
                                    _dl_fail.append({"kachel": _kachel, "error": str(_e)})

                        _prog.progress(1.0, text="Complete")
                        _zbuf.seek(0)
                        _zbytes = _zbuf.getvalue()

                        if _dl_ok:
                            _zmb = len(_zbytes) / (1024 * 1024)
                            _status.success(f"Downloaded {len(_dl_ok)} file(s) — {_zmb:.1f} MB ZIP")
                            st.download_button(
                                label=f"Save gml_tiles.zip ({len(_dl_ok)} files, {_zmb:.1f} MB)",
                                data=_zbytes,
                                file_name="gml_tiles.zip",
                                mime="application/zip",
                                use_container_width=True,
                                key="gml_tile_zip_dl",
                            )
                            with st.expander("Download details"):
                                for _d in _dl_ok:
                                    st.caption(f"✓ {_d['filename']}  ({_d['size_kb']:.0f} KB)")
                                for _f in _dl_fail:
                                    st.caption(f"✗ {_f['kachel']}: {_f['error']}")
                            st.info(
                                "If files are **.xml**, use the **📄 XML → GML** tool to rename them, "
                                "then upload to **GML → IFC**."
                            )
                        else:
                            _status.error("All downloads failed!")
                            for _f in _dl_fail:
                                st.error(f"✗ {_f['kachel']}: {_f['error']}")

        else:
            st.info("Upload the GeoJSON tile index to get started")
            with st.expander("About GML/XML Tile Downloader"):
                st.markdown("""
                Download GML or XML building/feature tiles from a geodata portal.

                **Steps:**
                1. Download the GeoJSON tile index from your geodata portal
                2. Upload it here
                3. Enter a center tile ID (9-digit kachel)
                4. Click **Select 3×3 grid** or click tiles individually on the map
                5. Click **Download files as ZIP**
                6. If files are **.xml**, use **XML → GML** to rename them
                7. Upload **.gml** files in the **GML → IFC** tab

                **Tile ID format:** `ZZXXXYYY` (zone 2 + easting km 3 + northing km 4)
                """)


# ============================================================================
# FOOTER
# ============================================================================
st.divider()
st.markdown(
    """
    <div style='text-align: center; color: gray; font-size: 0.8em; padding: 1em 0;'>
    GeoData Converter v1.2 | Built with Streamlit, IfcOpenShell & pyproj
    </div>
    """,
    unsafe_allow_html=True
)