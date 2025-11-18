# GML to IFC Converter - Streamlit App

A simple web application for converting GML (Geography Markup Language) files to IFC4X3 format.

## Features

- 🎯 **Simple Interface**: Single-page app with drag-and-drop file upload
- 📁 **Batch Processing**: Convert multiple GML files at once
- 📥 **Easy Download**: Download individual files or all as a ZIP archive
- ⚙️ **Configurable**: Adjust EPSG codes and MapConversion settings
- 📊 **Visual Feedback**: See coordinate bounds and processing status

## Installation

### Local Development

1. Clone the repository:
```bash
git clone https://github.com/YOUR_USERNAME/gml-to-ifc-converter.git
cd gml-to-ifc-converter
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run the app:
```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`

## Deployment

### Streamlit Community Cloud

1. Push your code to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub repository
4. Deploy!

### Docker

```bash
docker build -t gml-to-ifc-converter .
docker run -p 8501:8501 gml-to-ifc-converter
```

## Usage

1. **Upload GML Files**: Click "Browse files" or drag and drop your GML files
2. **Configure Settings** (optional): Expand the settings panel to adjust:
   - Default EPSG code
   - MapConversion toggle
3. **Download Results**: 
   - Single file: Direct download button
   - Multiple files: Download as ZIP or individual files

## Technical Details

- **Input Format**: GML files with polygon geometry
- **Output Format**: IFC4X3 with IfcBuildingElementProxy objects
- **Coordinate Systems**: Automatic EPSG detection or configurable default
- **Geometry**: Creates proper 3D surface models with face-based geometry

## Configuration

Edit the following constants in `app.py`:

```python
DEFAULT_EPSG_CODE = 25832  # Default EPSG code if not found in GML
USE_MAP_CONVERSION = False  # Enable/disable MapConversion
```

## Requirements

- Python 3.8+
- streamlit
- ifcopenshell

## License

MIT License

## Author

Victor Budinic