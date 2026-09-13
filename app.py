import streamlit as st
import folium
from folium.plugins import Draw, Geocoder
from streamlit_folium import st_folium
import urllib.request
import xml.etree.ElementTree as ET
import ezdxf
import os
import ssl
import tempfile
import io
import math

try:
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union
    import matplotlib.pyplot as plt
    import matplotlib.path as mpath
    import matplotlib.patches as mpatches
    import pydeck as pdk
    import numpy as np
    from PIL import Image
except ImportError:
    st.error("명령 프롬프트에서 'pip install shapely matplotlib pydeck numpy pillow'를 설치해주세요.")

MAPBOX_TOKEN = st.secrets["MAPBOX_TOKEN"]
st.set_page_config(page_title="Sitedia 스타일 다이어그램 추출기", layout="wide", initial_sidebar_state="expanded")

with st.sidebar:
    st.markdown("**🎨 웹사이트 테마 설정**")
    bg_color = st.color_picker("전체 배경 색상", "#1E1B18")
    panel_color = st.color_picker("패널 배경 색상", "#2A2421")
    btn_color = st.color_picker("버튼 색상", "#1B263B")
    border_color = st.color_picker("버튼 테두리 색상", "#5C4033")
    text_color = st.color_picker("텍스트 색상", "#EAEAEA")

custom_css = f"""
<style>
.stApp {{ background-color: {bg_color}; color: {text_color}; }}
[data-testid="stSidebar"] {{ background-color: {panel_color}; }}
.stButton>button, .stDownloadButton>button {{
    background-color: {btn_color}; color: {text_color};
    border: 2px solid {border_color}; border-radius: 8px; font-weight: bold;
}}
.stButton>button:hover, .stDownloadButton>button:hover {{ opacity: 0.8; border-color: {text_color}; }}
h1, h2, h3, p, span, div {{ color: {text_color} !important; }}
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

def add_geom(geom, layer, feature_list):
    if geom.is_empty: return
    if geom.geom_type == 'Polygon':
        feature_list.append({'layer': layer, 'ext': list(geom.exterior.coords), 'ints': [list(i.coords) for i in geom.interiors]})
    elif geom.geom_type == 'MultiPolygon':
        for g in geom.geoms:
            feature_list.append({'layer': layer, 'ext': list(g.exterior.coords), 'ints': [list(i.coords) for i in g.interiors]})

def get_osm_core_data(min_lon, min_lat, max_lon, max_lat, lon_ratio):
    url = f"https://api.openstreetmap.org/api/0.6/map?bbox={min_lon},{min_lat},{max_lon},{max_lat}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
            return parse_osm_xml(response.read(), lon_ratio)
    except Exception as e:
        st.error(f"OSM 데이터 접속 실패: {e}")
        return []

def parse_osm_xml(xml_data, lon_ratio):
    root = ET.fromstring(xml_data)
    nodes = {node.attrib['id']: (float(node.attrib['lon']) * 100000 * lon_ratio, float(node.attrib['lat']) * 100000) for node in root.findall('node')}
    
    features = []
    road_polygons = [] 
    
    for way in root.findall('way'):
        coords = [nodes[nd.attrib['ref']] for nd in way.findall('nd') if nd.attrib['ref'] in nodes]
        if len(coords) < 2: continue
        tags = {tag.attrib['k']: tag.attrib['v'] for tag in way.findall('tag')}
            
        if 'building' in tags:
            levels = int(tags.get('building:levels', '0')) if tags.get('building:levels', '0').isdigit() else 0
            layer = 'BLDG_HIGH' if levels >= 5 else 'BLDG_LOW'
            features.append({'layer': layer, 'ext': coords, 'ints': []})
            
        if 'highway' in tags:
            hw_type = tags.get('highway')
            if hw_type in ['primary', 'trunk']: w = 18.0       
            elif hw_type in ['secondary', 'tertiary']: w = 12.0 
            elif hw_type in ['residential']: w = 8.0           
            else: w = 5.0 
            road_polygons.append(LineString(coords).buffer(w / 2.0, cap_style=2, join_style=2))
            
        if 'landuse' in tags and tags.get('landuse') in ['forest', 'grass', 'meadow']:
            features.append({'layer': 'GREEN', 'ext': coords, 'ints': []})
            
        if 'leisure' in tags or tags.get('natural') == 'wood': 
            features.append({'layer': 'GREEN', 'ext': coords, 'ints': []})
            
        if 'waterway' in tags or tags.get('natural') == 'water':
            if 'waterway' in tags: add_geom(LineString(coords).buffer(15.0 if tags.get('waterway') == 'river' else 6.0 / 2.0, cap_style=2, join_style=2), 'WATER_POLY', features)
            else: features.append({'layer': 'WATER_POLY', 'ext': coords, 'ints': []})

    if road_polygons: add_geom(unary_union(road_polygons), 'ROAD_BLOCK', features)
    return features

def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile

def num2deg(xtile, ytile, zoom):
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg

def get_mapbox_contours(min_lon, min_lat, max_lon, max_lat, token, lon_ratio):
    zoom = 15
    cx, cy = (min_lon + max_lon) / 2, (min_lat + max_lat) / 2
    
    # 선택 영역의 중심점이 타일 내에서 어디쯤 위치하는지 소수점까지 계산
    n = 2.0 ** zoom
    xf = (cx + 180.0) / 360.0 * n
    yf = (1.0 - math.asinh(math.tan(math.radians(cy))) / math.pi) / 2.0 * n
    
    tx_c, ty_c = int(xf), int(yf)
    
    # [핵심] 9장이 아닌, 중심점과 가장 가까운 4장(2x2)의 타일만 똑똑하게 선택하여 속도 2배 이상 향상
    dx_list = [0, 1] if xf - tx_c > 0.5 else [-1, 0]
    dy_list = [0, 1] if yf - ty_c > 0.5 else [-1, 0]
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    elev_map = np.zeros((512, 512)) # 768x768 (9장) -> 512x512 (4장)으로 처리량 감소
    
    for i, dy in enumerate(dy_list):
        for j, dx in enumerate(dx_list):
            tx, ty = tx_c + dx, ty_c + dy
            url = f"https://api.mapbox.com/v4/mapbox.terrain-rgb/{zoom}/{tx}/{ty}.pngraw?access_token={token}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
                    img = Image.open(io.BytesIO(response.read())).convert('RGB')
                    arr = np.array(img).astype(np.float64)
                    e = -10000 + ((arr[:,:,0]*65536 + arr[:,:,1]*256 + arr[:,:,2]) * 0.1)
                    elev_map[i*256:(i+1)*256, j*256:(j+1)*256] = e
            except: pass
                
    lat_max_t, lon_min_t = num2deg(tx_c + dx_list[0], ty_c + dy_list[0], zoom)
    lat_min_t, lon_max_t = num2deg(tx_c + dx_list[-1] + 1, ty_c + dy_list[-1] + 1, zoom)
    
    x = np.linspace(lon_min_t, lon_max_t, 512)
    y = np.linspace(lat_min_t, lat_max_t, 512)
    X, Y = np.meshgrid(x, y)
    elev_map = np.flipud(elev_map) 
    
    fig_c, ax_c = plt.subplots()
    min_e, max_e = np.min(elev_map), np.max(elev_map)
    
    if max_e - min_e < 1: levels = np.arange(min_e - 2, max_e + 2, 0.5)
    else: levels = np.arange(math.floor(min_e), math.ceil(max_e) + 1, 1)
        
    cs = ax_c.contour(X, Y, elev_map, levels=levels)
    
    contours = []
    for i, val in enumerate(cs.levels):
        paths = cs.collections[i].get_paths() if hasattr(cs, 'collections') else [cs.get_paths()[i]]
        for path in paths:
            for poly in path.to_polygons(closed_only=False):
                scaled_ext = [(float(pt[0] * 100000 * lon_ratio), float(pt[1] * 100000)) for pt in poly]
                if len(scaled_ext) > 1:
                    contours.append({'layer': 'CONTOUR', 'ext': scaled_ext, 'ints': [], 'height': float(val)})
    plt.close(fig_c)
    
    elev_lookup = {
        'map': elev_map,
        'x_min': lon_min_t * 100000 * lon_ratio,
        'x_max': lon_max_t * 100000 * lon_ratio,
        'y_min': lat_min_t * 100000,
        'y_max': lat_max_t * 100000
    }
    return contours, elev_lookup

def create_highlight_diagram(features, target_layer, min_lon, min_lat, max_lon, max_lat, lon_ratio):
    fig, ax = plt.subplots(figsize=(6, 6), facecolor='#1A1A1F')
    ax.set_facecolor('#1A1A1F')

    highlight_colors = {'ROAD_BLOCK': '#FFD700', 'BLDG_HIGH': '#FF2A2A', 'BLDG_LOW': '#E0E0E0', 'GREEN': '#32CD32', 'WATER_POLY': '#1E90FF'}
    muted_color = '#454550'

    cx = ((min_lon + max_lon) / 2) * 100000 * lon_ratio
    cy = ((min_lat + max_lat) / 2) * 100000
    width = (max_lon - min_lon) * 100000 * lon_ratio
    height = (max_lat - min_lat) * 100000
    radius = min(width, height) / 2 * 0.95 

    clip_circle = mpatches.Circle((cx, cy), radius, transform=ax.transData)
    base_land = mpatches.Circle((cx, cy), radius, facecolor='#282832', edgecolor='none', zorder=0)
    base_land.set_clip_path(clip_circle)
    ax.add_patch(base_land)

    for item in features:
        layer, ext, ints = item['layer'], item['ext'], item['ints']
        
        if layer == 'CONTOUR':
            if target_layer == 'CONTOUR':
                h = item.get('height', 0)
                if h % 5 == 0: line, = ax.plot([pt[0] for pt in ext], [pt[1] for pt in ext], color='#FFFFFF', linewidth=1.5, zorder=6)
                else: line, = ax.plot([pt[0] for pt in ext], [pt[1] for pt in ext], color='#5D6D7E', linewidth=0.6, zorder=5)
                line.set_clip_path(clip_circle)
            continue
            
        color = highlight_colors.get(layer, '#FFFFFF') if layer == target_layer else muted_color
        zorder = 5 if layer == target_layer else 1

        vertices, codes = [], []
        vertices.extend(ext)
        codes.extend([mpath.Path.MOVETO] + [mpath.Path.LINETO] * (len(ext) - 1) + [mpath.Path.CLOSEPOLY])
        vertices.append(ext[0])
        for hole in ints:
            vertices.extend(hole)
            codes.extend([mpath.Path.MOVETO] + [mpath.Path.LINETO] * (len(hole) - 1) + [mpath.Path.CLOSEPOLY])
            vertices.append(hole[0])

        patch = mpatches.PathPatch(mpath.Path(vertices, codes), facecolor=color, edgecolor='none', antialiased=True, zorder=zorder)
        patch.set_clip_path(clip_circle)
        ax.add_patch(patch)

    border_circle = mpatches.Circle((cx, cy), radius, fill=False, edgecolor='#50505A', linewidth=2, zorder=10)
    ax.add_patch(border_circle)
    ax.set_xlim(cx - radius * 1.05, cx + radius * 1.05)
    ax.set_ylim(cy - radius * 1.05, cy + radius * 1.05)
    ax.set_aspect('equal')
    plt.axis('off')
    plt.tight_layout()
    return fig

def export_site_data_to_dxf(features, elev_lookup, is_3d=False, offset_x=0.0, offset_y=0.0):
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    colors = {'BLDG_HIGH': 1, 'BLDG_LOW': 8, 'ROAD_BLOCK': 2, 'GREEN': 3, 'WATER_POLY': 5, 'CONTOUR': 7}
    for name, color in colors.items(): 
        doc.layers.add(name, color=color)

    def get_z(x, y):
        if not elev_lookup: return 0.0
        em = elev_lookup['map']
        w, h = em.shape[1], em.shape[0]
        ix = int((x - elev_lookup['x_min']) / (elev_lookup['x_max'] - elev_lookup['x_min']) * (w - 1))
        iy = int((y - elev_lookup['y_min']) / (elev_lookup['y_max'] - elev_lookup['y_min']) * (h - 1))
        ix = max(0, min(w - 1, ix))
        iy = max(0, min(h - 1, iy))
        return float(em[iy, ix])

    for item in features:
        layer, ext, ints = item['layer'], item['ext'], item['ints']
        
        if layer == 'CONTOUR':
            shifted_ext = [(float(pt[0] - offset_x), float(pt[1] - offset_y)) for pt in ext]
            if is_3d:
                z_val = float(item.get('height', 0))
                pts_3d = [(pt[0], pt[1], z_val) for pt in shifted_ext]
                msp.add_polyline3d(pts_3d, dxfattribs={'layer': layer})
            else:
                msp.add_lwpolyline(shifted_ext, close=False, dxfattribs={'layer': layer})
            continue

        if is_3d and layer not in ['BLDG_HIGH', 'BLDG_LOW']:
            pts_3d = [(float(pt[0] - offset_x), float(pt[1] - offset_y), get_z(pt[0], pt[1])) for pt in ext]
            msp.add_polyline3d(pts_3d, dxfattribs={'layer': layer})
            for hole in ints:
                h_pts = [(float(pt[0] - offset_x), float(pt[1] - offset_y), get_z(pt[0], pt[1])) for pt in hole]
                msp.add_polyline3d(h_pts, dxfattribs={'layer': layer})
        else:
            shifted_ext = [(float(pt[0] - offset_x), float(pt[1] - offset_y)) for pt in ext]
            shifted_ints = [[(float(pt[0] - offset_x), float(pt[1] - offset_y)) for pt in hole] for hole in ints]
            
            thickness = 25.0 if is_3d and layer == 'BLDG_HIGH' else (8.0 if is_3d and layer == 'BLDG_LOW' else 0.0)
            
            closest_z = 0.0
            if is_3d:
                cx = sum(pt[0] for pt in ext) / len(ext)
                cy = sum(pt[1] for pt in ext) / len(ext)
                closest_z = get_z(cx, cy)
                
            pline = msp.add_lwpolyline(shifted_ext, close=True, dxfattribs={'layer': layer})
            if thickness > 0: pline.dxf.thickness = thickness
            if is_3d: pline.dxf.elevation = closest_z
            
            for hole in shifted_ints: 
                h_pline = msp.add_lwpolyline(hole, close=True, dxfattribs={'layer': layer})
                if thickness > 0: h_pline.dxf.thickness = thickness
                if is_3d: h_pline.dxf.elevation = closest_z
                
            if not is_3d:
                try:
                    hatch = msp.add_hatch(color=256, dxfattribs={'layer': layer})
                    hatch.paths.add_polyline_path(shifted_ext, is_closed=True)
                    for hole in shifted_ints: hatch.paths.add_polyline_path(hole, is_closed=True)
                except: pass

    tmp_dir = tempfile.gettempdir()
    file_name = "Site_Mass_3D.dxf" if is_3d else "Site_Diagram_2D.dxf"
    file_path = os.path.join(tmp_dir, file_name)
    doc.saveas(file_path)
    return file_path

st.title("📍 건축 대지 분석 자동화 툴 (6종 핵심 패널)")
st.markdown("**1. 영역 지정 (마우스 드래그)**")
m = folium.Map(location=[35.5425, 139.4445], zoom_start=16)
Geocoder().add_to(m)
Draw(export=False, draw_options={'polyline': False, 'polygon': False, 'circle': False, 'marker': False, 'circlemarker': False, 'rectangle': True}).add_to(m)
output = st_folium(m, width=1200, height=450)

if output and output.get("last_active_drawing"):
    coords = output["last_active_drawing"]["geometry"]["coordinates"][0]
    lons, lats = [c[0] for c in coords], [c[1] for c in coords]
    min_lon, min_lat, max_lon, max_lat = min(lons), min(lats), max(lons), max(lats)
    
    dynamic_center_lat = (min_lat + max_lat) / 2
    dynamic_lon_ratio = math.cos(math.radians(dynamic_center_lat))
    
    if st.button("🚀 6종 다이어그램 및 3D 데이터 추출", use_container_width=True):
        with st.spinner("OSM 데이터 및 Mapbox 지형 고도(API)를 분석 중입니다..."):
            features = get_osm_core_data(min_lon, min_lat, max_lon, max_lat, dynamic_lon_ratio)
            contours, elev_lookup = get_mapbox_contours(min_lon, min_lat, max_lon, max_lat, MAPBOX_TOKEN, dynamic_lon_ratio)
            features.extend(contours)
            
            if features:
                st.markdown("---")
                st.markdown("### 📊 다이어그램 범례 (Color Legend)")
                legend_html = f"""
                <div style="background-color: {panel_color}; padding: 15px; border-radius: 10px; display: flex; justify-content: space-around; flex-wrap: wrap; gap: 10px;">
                    <div style="display: flex; align-items: center;"><div style="width: 20px; height: 20px; background-color: #FFD700; border-radius: 4px; margin-right: 8px;"></div><b>도로망</b></div>
                    <div style="display: flex; align-items: center;"><div style="width: 20px; height: 20px; background-color: #FF2A2A; border-radius: 4px; margin-right: 8px;"></div><b>고층 건물</b></div>
                    <div style="display: flex; align-items: center;"><div style="width: 20px; height: 20px; background-color: #E0E0E0; border-radius: 4px; margin-right: 8px;"></div><b>저층 건물</b></div>
                    <div style="display: flex; align-items: center;"><div style="width: 20px; height: 20px; background-color: #32CD32; border-radius: 4px; margin-right: 8px;"></div><b>녹지/공원</b></div>
                    <div style="display: flex; align-items: center;"><div style="width: 20px; height: 20px; background-color: #1E90FF; border-radius: 4px; margin-right: 8px;"></div><b>수공간</b></div>
                    <div style="display: flex; align-items: center;"><div style="width: 20px; height: 20px; background-color: #FFFFFF; border-radius: 4px; margin-right: 8px;"></div><b>등고선(1m/5m)</b></div>
                </div>
                """
                st.markdown(legend_html, unsafe_allow_html=True)
                
                st.markdown("<br>### 🖼️ 2D 개별 분석 패널", unsafe_allow_html=True)
                
                row1 = st.columns(3)
                layers_r1 = ['ROAD_BLOCK', 'BLDG_HIGH', 'BLDG_LOW']
                titles_r1 = ["🛣️ 도로망", "🏢 고층 건물", "🏠 저층 건물"]
                for i, col in enumerate(row1):
                    with col:
                        st.markdown(f"<div style='text-align: center; margin-bottom:10px;'><b>{titles_r1[i]}</b></div>", unsafe_allow_html=True)
                        fig = create_highlight_diagram(features, layers_r1[i], min_lon, min_lat, max_lon, max_lat, dynamic_lon_ratio)
                        st.pyplot(fig)
                
                row2 = st.columns(3)
                layers_r2 = ['GREEN', 'WATER_POLY', 'CONTOUR']
                titles_r2 = ["🌳 녹지 축", "💧 수공간", "⛰️ 지형 등고선"]
                for i, col in enumerate(row2):
                    with col:
                        st.markdown(f"<div style='text-align: center; margin-bottom:10px;'><b>{titles_r2[i]}</b></div>", unsafe_allow_html=True)
                        fig = create_highlight_diagram(features, layers_r2[i], min_lon, min_lat, max_lon, max_lat, dynamic_lon_ratio)
                        st.pyplot(fig)
                
                st.markdown("---")
                st.markdown("### 🧊 3D 매스 및 지형 등고선 뷰")
                
                polygons_3d = []
                lines_3d = []
                
                for item in features:
                    layer = item['layer']
                    unscaled_ext = [[x / (100000 * dynamic_lon_ratio), y / 100000] for x, y in item['ext']]
                    
                    if layer == 'CONTOUR':
                        h = item.get('height', 0)
                        lines_3d.append({'path': unscaled_ext, 'color': [255, 255, 255, 220] if h % 5 == 0 else [120, 130, 140, 150]})
                        continue
                        
                    unscaled_ints = [[[hx / (100000 * dynamic_lon_ratio), hy / 100000] for hx, hy in hole] for hole in item.get('ints', [])]
                    geom_3d = [unscaled_ext] + unscaled_ints
                    
                    height, color = 0, [0,0,0,0]
                    if layer == 'BLDG_HIGH': height, color = 25, [255, 42, 42, 220]
                    elif layer == 'BLDG_LOW': height, color = 8, [224, 224, 224, 220]
                    elif layer == 'ROAD_BLOCK': height, color = 0.5, [255, 215, 0, 180]
                    elif layer == 'GREEN': height, color = 0.2, [50, 205, 50, 180]
                    elif layer == 'WATER_POLY': height, color = 0.1, [30, 144, 255, 180]
                    
                    if height > 0:
                        polygons_3d.append({'polygon': geom_3d, 'height': height, 'color': color})
                
                layer_3d_poly = pdk.Layer('PolygonLayer', data=polygons_3d, get_polygon='polygon', get_fill_color='color', get_elevation='height', extruded=True, wireframe=True)
                layer_3d_line = pdk.Layer('PathLayer', data=lines_3d, get_path='path', get_color='color', width_scale=1, width_min_pixels=1.5)
                view_state = pdk.ViewState(longitude=(min_lon + max_lon) / 2, latitude=(min_lat + max_lat) / 2, zoom=15.5, pitch=50, bearing=-15)
                st.pydeck_chart(pdk.Deck(layers=[layer_3d_poly, layer_3d_line], initial_view_state=view_state, map_provider='carto', map_style='dark'), use_container_width=True)

                st.markdown("---")
                
                offset_x = ((min_lon + max_lon) / 2) * 100000 * dynamic_lon_ratio
                offset_y = ((min_lat + max_lat) / 2) * 100000
                
                col_dxf1, col_dxf2 = st.columns(2)
                with col_dxf1:
                    dxf_2d = export_site_data_to_dxf(features, elev_lookup, is_3d=False, offset_x=offset_x, offset_y=offset_y)
                    with open(dxf_2d, "rb") as file: st.download_button("📥 2D 다이어그램 (일러스트/피그마)", data=file, file_name="Site_Diagram_2D.dxf", mime="application/dxf", use_container_width=True)
                with col_dxf2:
                    dxf_3d = export_site_data_to_dxf(features, elev_lookup, is_3d=True, offset_x=offset_x, offset_y=offset_y)
                    with open(dxf_3d, "rb") as file: st.download_button("📦 3D 매스/지형 (라이노/오토캐드/블렌더)", data=file, file_name="Site_Mass_3D.dxf", mime="application/dxf", use_container_width=True)
