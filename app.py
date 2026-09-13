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
    import pandas as pd
    import plotly.express as px
    import requests
except ImportError:
    st.error("명령 프롬프트에서 'pip install shapely matplotlib pydeck numpy pillow pandas plotly requests'를 설치해주세요.")

# ==========================================
# 🔒 보안 금고 연동 (API 키 노출 방지)
# ==========================================
try:
    MAPBOX_TOKEN = st.secrets["MAPBOX_TOKEN"]
except:
    MAPBOX_TOKEN = "pk.eyJ1IjoibGVlamltaW4iLCJhIjoiY210eXN2ZW45MDF6bjJ3cTUydW14enNjZyJ9.XnNv2cPEEEmgeEaJyhtJ2Q"

st.set_page_config(page_title="Sitedia 스타일 다이어그램 추출기", layout="wide", initial_sidebar_state="expanded")

with st.sidebar:
    st.markdown("**🎨 웹사이트 테마 설정**")
    bg_color = st.color_picker("전체 배경 색상", "#1E1B18")
    panel_color = st.color_picker("패널 배경 색상", "#2A2421")
    btn_color = st.color_picker("버튼 색상", "#1B263B")
    border_color = st.color_picker("버튼 테두리 색상", "#5C4033")
    text_color = st.color_picker("텍스트 색상", "#EAEAEA")
    
    st.markdown("---")
    st.markdown("**🌞 실시간 그림자/음영 시뮬레이션**")
    sim_season = st.selectbox("계절 선택", ["하지 (6월)", "춘/추분 (3,9월)", "동지 (12월)"])
    sim_hour = st.slider("시간대", 6, 18, 12)

    st.markdown("---")
    st.markdown("**🌬️ 미기후 분석 (바람장미)**")
    show_wind = st.checkbox("최근 90일 바람장미(Wind Rose) 표시", value=True)

    st.markdown("---")
    st.markdown("**🏙️ PLATEAU 3D 연동 (스트리밍)**")
    st.caption("고정밀 도시 3D 모델을 불러옵니다. (렌더링에 몇 초 소요될 수 있음)")
    use_plateau = st.checkbox("Project PLATEAU 매스 띄우기", value=False)
    plateau_url = st.text_input("3D Tiles URL", "https://plateau.geospatial.jp/main/data/3d-tiles/bldg/13100_tokyo/13100_tokyo.json")

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

# ==========================================
# [추가기능 2] 미기후 분석 (풍배도 생성)
# ==========================================
def create_wind_rose(lat, lon):
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&past_days=90&hourly=windspeed_10m,winddirection_10m"
        res = requests.get(url, timeout=10)
        data = res.json()
        df = pd.DataFrame(data['hourly']).dropna()
        
        bins = [0, 2, 4, 6, 8, 10, 100]
        labels = ['0-2 m/s', '2-4 m/s', '4-6 m/s', '6-8 m/s', '8-10 m/s', '>10 m/s']
        df['Speed'] = pd.cut(df['windspeed_10m'], bins=bins, labels=labels, right=False)
        
        dir_bins = np.arange(-11.25, 371.25, 22.5)
        dir_labels = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW', 'N2']
        df['Dir'] = pd.cut(df['winddirection_10m'], bins=dir_bins, labels=dir_labels)
        df['Dir'] = df['Dir'].replace('N2', 'N')
        
        counts = df.groupby(['Dir', 'Speed']).size().reset_index(name='Freq')
        counts['Freq'] = counts['Freq'] / counts['Freq'].sum() * 100
        
        fig = px.bar_polar(counts, r="Freq", theta="Dir", color="Speed", template="plotly_dark",
                           color_discrete_sequence=px.colors.sequential.Plasma,
                           title=f"대지 주변 90일 풍배도 (Wind Rose)")
        fig.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', margin=dict(t=50, b=20, l=20, r=20))
        return fig
    except Exception as e:
        st.error("풍배도 데이터를 불러오지 못했습니다.")
        return None

def calc_real_distance(lon1, lat1, lon2, lat2):
    R = 6371.0 
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

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
            w = 18.0 if hw_type in ['primary', 'trunk'] else 12.0 if hw_type in ['secondary', 'tertiary'] else 8.0 if hw_type in ['residential'] else 5.0 
            road_polygons.append(LineString(coords).buffer(w / 2.0, cap_style=2, join_style=2))
            
        if 'landuse' in tags and tags.get('landuse') in ['forest', 'grass', 'meadow']:
            features.append({'layer': 'GREEN', 'ext': coords, 'ints': []})
            
        if 'leisure' in tags or tags.get('natural') == 'wood': 
            features.append({'layer': 'GREEN', 'ext': coords, 'ints': []})
            
        if 'waterway' in tags or tags.get('natural') == 'water':
            add_geom(LineString(coords).buffer(15.0 if tags.get('waterway') == 'river' else 3.0, cap_style=2, join_style=2), 'WATER_POLY', features)

    if road_polygons: add_geom(unary_union(road_polygons), 'ROAD_BLOCK', features)
    return features

def num2deg(xtile, ytile, zoom):
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg

def get_mapbox_contours(min_lon, min_lat, max_lon, max_lat, token, lon_ratio):
    zoom = 15
    cx, cy = (min_lon + max_lon) / 2, (min_lat + max_lat) / 2
    n = 2.0 ** zoom
    xf = (cx + 180.0) / 360.0 * n
    yf = (1.0 - math.asinh(math.tan(math.radians(cy))) / math.pi) / 2.0 * n
    
    tx_c, ty_c = int(xf), int(yf)
    dx_list = [0, 1] if xf - tx_c > 0.5 else [-1, 0]
    dy_list = [0, 1] if yf - ty_c > 0.5 else [-1, 0]
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    elev_map = np.zeros((512, 512)) 
    
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
    levels = np.arange(min_e - 2, max_e + 2, 0.5) if max_e - min_e < 1 else np.arange(math.floor(min_e), math.ceil(max_e) + 1, 1)
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
    
    return contours, {'map': elev_map, 'x_min': lon_min_t * 100000 * lon_ratio, 'x_max': lon_max_t * 100000 * lon_ratio, 'y_min': lat_min_t * 100000, 'y_max': lat_max_t * 100000}

def get_z_val(x, y, lookup):
    if not lookup: return 0.0
    em = lookup['map']
    w, h = em.shape[1], em.shape[0]
    ix = int((x - lookup['x_min']) / (lookup['x_max'] - lookup['x_min']) * (w - 1))
    iy = int((y - lookup['y_min']) / (lookup['y_max'] - lookup['y_min']) * (h - 1))
    return float(em[max(0, min(h - 1, iy)), max(0, min(w - 1, ix))])

st.title("📍 건축 대지 분석 자동화 툴 (6종 핵심 패널)")
st.markdown("**1. 영역 지정 (사각형 드래그) 또는 단면 확인 (선 긋기)**")

m = folium.Map(location=[35.5425, 139.4445], zoom_start=16)
Geocoder().add_to(m)
Draw(export=False, draw_options={'polyline': True, 'polygon': False, 'circle': False, 'marker': False, 'circlemarker': False, 'rectangle': True}).add_to(m)
output = st_folium(m, width=1200, height=450)

if output and output.get("last_active_drawing"):
    geom_type = output["last_active_drawing"]["geometry"]["type"]
    coords = output["last_active_drawing"]["geometry"]["coordinates"]
    
    if geom_type == "Polygon":
        coords = coords[0]
        lons, lats = [c[0] for c in coords], [c[1] for c in coords]
        min_lon, min_lat, max_lon, max_lat = min(lons), min(lats), max(lons), max(lats)
        
        width_km = calc_real_distance(min_lon, (min_lat+max_lat)/2, max_lon, (min_lat+max_lat)/2)
        height_km = calc_real_distance((min_lon+max_lon)/2, min_lat, (min_lon+max_lon)/2, max_lat)
        st.info(f"📐 **선택된 대지 크기:** 가로 약 **{width_km*1000:.0f}m** × 세로 약 **{height_km*1000:.0f}m**")
        
        dynamic_lon_ratio = math.cos(math.radians((min_lat + max_lat) / 2))
        
        if st.button("🚀 6종 다이어그램 및 3D 데이터 추출", use_container_width=True):
            with st.spinner("OSM 데이터 및 지형/기후 데이터를 분석 중입니다..."):
                features = get_osm_core_data(min_lon, min_lat, max_lon, max_lat, dynamic_lon_ratio)
                contours, elev_lookup = get_mapbox_contours(min_lon, min_lat, max_lon, max_lat, MAPBOX_TOKEN, dynamic_lon_ratio)
                features.extend(contours)
                
                st.markdown("---")
                # 풍배도 차트 렌더링
                if show_wind:
                    st.markdown("### 🌬️ 미기후 분석 (풍향/풍속 시뮬레이션)")
                    wind_fig = create_wind_rose((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
                    if wind_fig: st.plotly_chart(wind_fig, use_container_width=True)
                    st.markdown("---")
                    
                st.markdown("### 🧊 3D 매스 및 지형 뷰 (PLATEAU 연동 포함)")
                polygons_3d = []
                lines_3d = []
                hour_factor = abs(12 - sim_hour) / 6.0 
                darken = int(hour_factor * 80)
                
                for item in features:
                    layer = item['layer']
                    if layer == 'CONTOUR':
                        h = item.get('height', 0)
                        lines_3d.append({'path': [[x / (100000 * dynamic_lon_ratio), y / 100000, h] for x, y in item['ext']], 'color': [255, 255, 255, 220] if h % 5 == 0 else [120, 130, 140, 150]})
                        continue
                    
                    cx_poly = sum(pt[0] for pt in item['ext']) / len(item['ext'])
                    cy_poly = sum(pt[1] for pt in item['ext']) / len(item['ext'])
                    base_z = get_z_val(cx_poly, cy_poly, elev_lookup)
                        
                    geom_3d = [[[x / (100000 * dynamic_lon_ratio), y / 100000, base_z] for x, y in item['ext']]]
                    
                    height, color = 0, [0,0,0,0]
                    if layer == 'BLDG_HIGH': height, color = 25, [max(0, 255-darken), max(0, 42-darken//2), max(0, 42-darken//2), 220]
                    elif layer == 'BLDG_LOW': height, color = 8, [max(0, 224-darken), max(0, 224-darken), max(0, 224-darken), 220]
                    elif layer == 'ROAD_BLOCK': height, color = 0.5, [255, 215, 0, 180]
                    elif layer == 'GREEN': height, color = 0.2, [50, 205, 50, 180]
                    elif layer == 'WATER_POLY': height, color = 0.1, [30, 144, 255, 180]
                    
                    if height > 0 and not (use_plateau and layer in ['BLDG_HIGH', 'BLDG_LOW']): 
                        # 플래토 사용 시 기본 OSM 건물은 숨김 처리
                        polygons_3d.append({'polygon': geom_3d, 'height': height, 'color': color})
                
                deck_layers = [
                    pdk.Layer('PolygonLayer', data=polygons_3d, get_polygon='polygon', get_fill_color='color', get_elevation='height', extruded=True, wireframe=True),
                    pdk.Layer('PathLayer', data=lines_3d, get_path='path', get_color='color', width_scale=1, width_min_pixels=1.5)
                ]
                
                # [추가기능 3] PLATEAU 타일 스트리밍 (pydeck Tile3DLayer)
                if use_plateau:
                    plateau_layer = pdk.Layer(
                        "Tile3DLayer",
                        data=plateau_url,
                        get_point_color=[255, 255, 255, 255],
                    )
                    deck_layers.append(plateau_layer)
                
                view_state = pdk.ViewState(longitude=(min_lon + max_lon) / 2, latitude=(min_lat + max_lat) / 2, zoom=15.5, pitch=40 + (hour_factor * 30), bearing=(sim_hour - 12) * 15)
                st.pydeck_chart(pdk.Deck(layers=deck_layers, initial_view_state=view_state, map_provider='carto', map_style='dark'), use_container_width=True)
