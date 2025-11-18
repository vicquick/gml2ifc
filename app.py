#!/usr/bin/env python3
"""
GML to IFC Converter - Streamlit App
Simple web interface for converting GML files to IFC4X3 format
"""

import streamlit as st
from pathlib import Path
import tempfile
import os

# Import converter functions
from gml_converter import convert_gml_to_ifc_bytes

# ============================================================================
# PAGE CONFIGURATION
# ============================================================================
st.set_page_config(
    page_title="GML to IFC Converter",
    page_icon="🏗️",
    layout="centered"
)

# ============================================================================
# HEADER
# ============================================================================
st.title("🏗️ GML to IFC Converter")
st.caption("powered by Streamlit + IfcOpenShell")
st.markdown("Convert GML (Geography Markup Language) files to IFC4X3 format")

st.divider()

# ============================================================================
# SETTINGS
# ============================================================================
with st.expander("⚙️ Settings"):
    use_map_conversion = st.checkbox(
        "Enable MapConversion",
        value=False,
        help="Enable IfcMapConversion for georeferencing (may cause coordinate issues in some viewers)"
    )
    
    default_epsg = st.number_input(
        "Default EPSG Code",
        value=25832,
        min_value=1,
        max_value=99999,
        help="Default EPSG code to use if not found in GML file (EPSG:25832 = ETRS89 / UTM zone 32N)"
    )

st.divider()

# ============================================================================
# FILE UPLOAD
# ============================================================================
st.subheader("📂 Upload GML File(s)")

uploaded_files = st.file_uploader(
    "Select one or more GML files",
    type=['gml', 'GML'],
    accept_multiple_files=True,
    help="Upload GML files containing polygon geometry"
)

# ============================================================================
# PROCESSING
# ============================================================================
if uploaded_files:
    st.divider()
    st.subheader(f"🔄 Processing {len(uploaded_files)} file(s)")
    
    # Store converted files
    converted_files = {}
    
    for idx, uploaded_file in enumerate(uploaded_files, 1):
        with st.container():
            col1, col2 = st.columns([3, 1])
            
            with col1:
                st.write(f"**{idx}. {uploaded_file.name}**")
            
            with col2:
                file_size = len(uploaded_file.getvalue()) / 1024  # KB
                st.caption(f"{file_size:.1f} KB")
            
            # Progress indicator
            progress_placeholder = st.empty()
            info_placeholder = st.empty()
            
            try:
                with st.spinner(f"Converting..."):
                    # Read file content
                    gml_content = uploaded_file.read()
                    
                    # Convert to IFC
                    result = convert_gml_to_ifc_bytes(
                        gml_content=gml_content,
                        filename=uploaded_file.name,
                        default_epsg=default_epsg,
                        use_map_conversion=use_map_conversion
                    )
                    
                    if result['success']:
                        output_filename = Path(uploaded_file.name).stem + ".ifc"
                        converted_files[output_filename] = result['ifc_bytes']
                        
                        # Show success info
                        info_placeholder.success(
                            f"✓ Converted • {result['num_polygons']} polygon(s) • EPSG:{result['epsg_code']}"
                        )
                        
                        # Show coordinate bounds in expandable section
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
    
    # ========================================================================
    # DOWNLOAD SECTION
    # ========================================================================
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
            import io
            import zipfile
            
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
    # ========================================================================
    # INSTRUCTIONS
    # ========================================================================
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
# FOOTER
# ============================================================================
st.divider()
st.markdown(
    """
    <div style='text-align: center; color: gray; font-size: 0.8em; padding: 1em 0;'>
    GML to IFC Converter v1.0 | Built with Streamlit + IfcOpenShell
    </div>
    """,
    unsafe_allow_html=True
)