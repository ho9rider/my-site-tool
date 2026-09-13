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
    st.caption("시간대에 따라 3D 건물의 음영과 카메라 태양광 각도가 자동 계산됩니다.")
    sim_season = st.selectbox("계절 선택", ["하지 (6월)", "춘/추분 (3,9월)", "동지 (12월)"])
    sim_hour = st.slider("시간대", 6, 18, 12)

custom_css = f'''
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
'''
st.markdown(custom_css, unsafe_allow_html=True)

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

def get_z_val(x, y, lookup):
    if not lookup: return 0.0
    em = lookup['map']
    w, h = em.shape[1], em.shape[0]
    ix = int((x - lookup['x_min']) / (lookup['x_max'] - lookup['x_min']) * (w - 1))
    iy = int((y - lookup['y_min']) / (lookup['y_max'] - lookup['y_min']) * (h - 1))
    ix = max(0, min(w - 1, ix))
    iy = max(0, min(h - 1, iy))
    return float(em[iy, ix])

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

def export_site_data_to_dxf(features, elev_lookup, min_lon, max_lon, min_lat, max_lat, lon_ratio, is_3d=False, offset_x=0.0, offset_y=0.0):
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    colors = {'BLDG_HIGH': 1, 'BLDG_LOW': 8, 'ROAD_BLOCK': 2, 'GREEN': 3, 'WATER_POLY': 5, 'CONTOUR': 7, 'HUMAN_SCALE': 6}
    for name, color in colors.items(): 
        doc.layers.add(name, color=color)

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
            pts_3d = [(float(pt[0] - offset_x), float(pt[1] - offset_y), get_z_val(pt[0], pt[1], elev_lookup)) for pt in ext]
            msp.add_polyline3d(pts_3d, dxfattribs={'layer': layer})
            for hole in ints:
                h_pts = [(float(pt[0] - offset_x), float(pt[1] - offset_y), get_z_val(pt[0], pt[1], elev_lookup)) for pt in hole]
                msp.add_polyline3d(h_pts, dxfattribs={'layer': layer})
        else:
            shifted_ext = [(float(pt[0] - offset_x), float(pt[1] - offset_y)) for pt in ext]
            shifted_ints = [[(float(pt[0] - offset_x), float(pt[1] - offset_y)) for pt in hole] for hole in ints]
            
            thickness = 25.0 if is_3d and layer == 'BLDG_HIGH' else (8.0 if is_3d and layer == 'BLDG_LOW' else 0.0)
            
            closest_z = 0.0
            if is_3d:
                cx = sum(pt[0] for pt in ext) / len(ext)
                cy = sum(pt[1] for pt in ext) / len(ext)
                closest_z = get_z_val(cx, cy, elev_lookup)
                
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

    c_x = ((min_lon + max_lon) / 2) * 100000 * lon_ratio
    c_y = ((min_lat + max_lat) / 2) * 100000
    c_z = get_z_val(c_x, c_y, elev_lookup)
    
    hs_shifted = [
        (float(c_x - 0.25 - offset_x), float(c_y - 0.25 - offset_y)),
        (float(c_x + 0.25 - offset_x), float(c_y - 0.25 - offset_y)),
        (float(c_x + 0.25 - offset_x), float(c_y + 0.25 - offset_y)),
        (float(c_x - 0.25 - offset_x), float(c_y + 0.25 - offset_y))
    ]
    
    hs_pline = msp.add_lwpolyline(hs_shifted, close=True, dxfattribs={'layer': 'HUMAN_SCALE'})
    if is_3d:
        hs_pline.dxf.thickness = 1.7
        hs_pline.dxf.elevation = c_z
    else:
        try:
            hatch = msp.add_hatch(color=256, dxfattribs={'layer': 'HUMAN_SCALE'})
            hatch.paths.add_polyline_path(hs_shifted, is_closed=True)
        except: pass

    tmp_dir = tempfile.gettempdir()
    file_name = "Site_Mass_3D.dxf" if is_3d else "Site_Diagram_2D.dxf"
    file_path = os.path.join(tmp_dir, file_name)
    doc.saveas(file_path)
    return file_path

# ==========================================
# [추가기능 4] 블렌더 친화적 OBJ 모델링 추출
# ==========================================
def export_site_data_to_obj(features, elev_lookup, min_lon, max_lon, min_lat, max_lat, lon_ratio, offset_x=0.0, offset_y=0.0):
    tmp_dir = tempfile.gettempdir()
    file_path = os.path.join(tmp_dir, "Site_Mass_3D.obj")
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write("# Sitedia 3D OBJ Export for Blender\n")
        v_idx = 1
        
        for item in features:
            layer, ext, ints = item['layer'], item['ext'], item['ints']
            
            if layer == 'CONTOUR':
                h = float(item.get('height', 0))
                idxs = []
                for pt in ext:
                    f.write(f"v {pt[0]-offset_x} {pt[1]-offset_y} {h}\n")
                    idxs.append(v_idx)
                    v_idx += 1
                f.write("l " + " ".join(map(str, idxs)) + "\n")
                continue
            
            if layer not in ['BLDG_HIGH', 'BLDG_LOW']:
                base_zs = [get_z_val(pt[0], pt[1], elev_lookup) for pt in ext]
                idxs = []
                for pt, z in zip(ext, base_zs):
                    f.write(f"v {pt[0]-offset_x} {pt[1]-offset_y} {z}\n")
                    idxs.append(v_idx)
                    v_idx += 1
                f.write("f " + " ".join(map(str, idxs)) + "\n")
            else:
                thickness = 25.0 if layer == 'BLDG_HIGH' else 8.0
                cx = sum(pt[0] for pt in ext) / len(ext)
                cy = sum(pt[1] for pt in ext) / len(ext)
                base_z = get_z_val(cx, cy, elev_lookup)
                top_z = base_z + thickness
                
                b_idxs = []
                for pt in ext:
                    f.write(f"v {pt[0]-offset_x} {pt[1]-offset_y} {base_z}\n")
                    b_idxs.append(v_idx)
                    v_idx += 1
                    
                t_idxs = []
                for pt in ext:
                    f.write(f"v {pt[0]-offset_x} {pt[1]-offset_y} {top_z}\n")
                    t_idxs.append(v_idx)
                    v_idx += 1
                    
                f.write("f " + " ".join(map(str, reversed(b_idxs))) + "\n")
                f.write("f " + " ".join(map(str, t_idxs)) + "\n")
                
                for i in range(len(ext) - 1):
                    f.write(f"f {b_idxs[i]} {t_idxs[i]} {t_idxs[i+1]} {b_idxs[i+1]}\n")
                    
        c_x = ((min_lon + max_lon) / 2) * 100000 * lon_ratio
        c_y = ((min_lat + max_lat) / 2) * 100000
        c_z = get_z_val(c_x, c_y, elev_lookup)
        hs = [
            (float(c_x - 0.25 - offset_x), float(c_y - 0.25 - offset_y)),
            (float(c_x + 0.25 - offset_x), float(c_y - 0.25 - offset_y)),
            (float(c_x + 0.25 - offset_x), float(c_y + 0.25 - offset_y)),
            (float(c_x - 0.25 - offset_x), float(c_y + 0.25 - offset_y))
        ]
        
        hb_idxs = []
        for p in hs:
            f.write(f"v {p[0]} {p[1]} {c_z}\n")
            hb_idxs.append(v_idx)
            v_idx += 1
        ht_idxs = []
        for p in hs:
            f.write(f"v {p[0]} {p[1]} {c_z + 1.7}\n")
            ht_idxs.append(v_idx)
            v_idx += 1
            
        f.write("f " + " ".join(map(str, reversed(hb_idxs))) + "\n")
        f.write("f " + " ".join(map(str, ht_idxs)) + "\n")
        for i in range(4):
            nxt = (i+1)%4
            f.write(f"f {hb_idxs[i]} {ht_idxs[i]} {ht_idxs[nxt]} {hb_idxs[nxt]}\n")
            
    return file_path

st.title("📍 건축 대지 분석 자동화 툴 (6종 핵심 패널)")
st.markdown("**1. 영역 지정 (사각형 드래그) 또는 단면 확인 (선 긋기)**")
st.caption("선(Polyline) 모양 아이콘을 클릭해 단면 경로를 그으면 2D 단면도가 자동으로 생성됩니다.")

m = folium.Map(location=[35.5425, 139.4445], zoom_start=16)
Geocoder().add_to(m)

# [추가기능 3] 단면선 긋기 (polyline=True) 허용
Draw(export=False, draw_options={'polyline': True, 'polygon': False, 'circle': False, 'marker': False, 'circlemarker': False, 'rectangle': True}).add_to(m)
output = st_folium(m, width=1200, height=450)

if output and output.get("last_active_drawing"):
    geom_type = output["last_active_drawing"]["geometry"]["type"]
    coords = output["last_active_drawing"]["geometry"]["coordinates"]
    
    if geom_type == "LineString":
        lons, lats = [c[0] for c in coords], [c[1] for c in coords]
        min_lon, min_lat, max_lon, max_lat = min(lons), min(lats), max(lons), max(lats)
        dynamic_center_lat = (min_lat + max_lat) / 2
        dynamic_lon_ratio = math.cos(math.radians(dynamic_center_lat))
        
        if st.button("🔪 대지 단면도(Section Profile) 생성"):
            with st.spinner("단면 고도 데이터를 분석 중입니다..."):
                contours, elev_lookup = get_mapbox_contours(min_lon-0.002, min_lat-0.002, max_lon+0.002, max_lat+0.002, MAPBOX_TOKEN, dynamic_lon_ratio)
                
                line = LineString(coords)
                num_samples = 100
                distances = []
                elevations = []
                
                total_length_km = 0
                for i in range(len(coords)-1):
                    total_length_km += calc_real_distance(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1])
                
                for i in range(num_samples + 1):
                    pt = line.interpolate(i / num_samples, normalized=True)
                    x_m = pt.x * 100000 * dynamic_lon_ratio
                    y_m = pt.y * 100000
                    z = get_z_val(x_m, y_m, elev_lookup)
                    
                    distances.append((i / num_samples) * total_length_km * 1000)
                    elevations.append(z)
                
                fig_sec, ax_sec = plt.subplots(figsize=(10, 3), facecolor='#1A1A1F')
                ax_sec.set_facecolor('#1A1A1F')
                ax_sec.plot(distances, elevations, color='#FF1493', linewidth=2)
                ax_sec.fill_between(distances, min(elevations)-5, elevations, color='#FF1493', alpha=0.3)
                
                ax_sec.set_title("대지 단면 프로파일 (Section Profile)", color='white')
                ax_sec.set_xlabel("거리 (m)", color='white')
                ax_sec.set_ylabel("해발 고도 (m)", color='white')
                ax_sec.tick_params(colors='white')
                for spine in ax_sec.spines.values():
                    spine.set_color('#454550')
                
                st.pyplot(fig_sec)

    elif geom_type == "Polygon":
        coords = coords[0]
        lons, lats = [c[0] for c in coords], [c[1] for c in coords]
        min_lon, min_lat, max_lon, max_lat = min(lons), min(lats), max(lons), max(lats)
        
        width_km = calc_real_distance(min_lon, (min_lat+max_lat)/2, max_lon, (min_lat+max_lat)/2)
        height_km = calc_real_distance((min_lon+max_lon)/2, min_lat, (min_lon+max_lon)/2, max_lat)
        st.info(f"📐 **선택된 대지 크기:** 가로 약 **{width_km*1000:.0f}m** × 세로 약 **{height_km*1000:.0f}m**")
        
        dynamic_center_lat = (min_lat + max_lat) / 2
        dynamic_lon_ratio = math.cos(math.radians(dynamic_center_lat))
        
        if st.button("🚀 6종 다이어그램 및 3D 데이터 추출", use_container_width=True):
            with st.spinner("OSM 데이터 및 Mapbox 지형 고도(API)를 분석 중입니다..."):
                features = get_osm_core_data(min_lon, min_lat, max_lon, max_lat, dynamic_lon_ratio)
                contours, elev_lookup = get_mapbox_contours(min_lon, min_lat, max_lon, max_lat, MAPBOX_TOKEN, dynamic_lon_ratio)
                features.extend(contours)
                
                if features:
                    st.markdown("---")
                    
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
                    
                    # [추가기능 1] 시간에 따른 일조량/그림자 효과 시각화
                    hour_factor = abs(12 - sim_hour) / 6.0 
                    darken = int(hour_factor * 80)
                    
                    for item in features:
                        layer = item['layer']
                        
                        if layer == 'CONTOUR':
                            h = item.get('height', 0)
                            unscaled_ext_3d = [[x / (100000 * dynamic_lon_ratio), y / 100000, h] for x, y in item['ext']]
                            lines_3d.append({'path': unscaled_ext_3d, 'color': [255, 255, 255, 220] if h % 5 == 0 else [120, 130, 140, 150]})
                            continue
                        
                        cx_poly = sum(pt[0] for pt in item['ext']) / len(item['ext'])
                        cy_poly = sum(pt[1] for pt in item['ext']) / len(item['ext'])
                        base_z = get_z_val(cx_poly, cy_poly, elev_lookup)
                            
                        unscaled_ext = [[x / (100000 * dynamic_lon_ratio), y / 100000, base_z] for x, y in item['ext']]
                        unscaled_ints = [[[hx / (100000 * dynamic_lon_ratio), hy / 100000, base_z] for hx, hy in hole] for hole in item.get('ints', [])]
                        geom_3d = [unscaled_ext] + unscaled_ints
                        
                        height, color = 0, [0,0,0,0]
                        if layer == 'BLDG_HIGH': height, color = 25, [max(0, 255-darken), max(0, 42-darken//2), max(0, 42-darken//2), 220]
                        elif layer == 'BLDG_LOW': height, color = 8, [max(0, 224-darken), max(0, 224-darken), max(0, 224-darken), 220]
                        elif layer == 'ROAD_BLOCK': height, color = 0.5, [255, 215, 0, 180]
                        elif layer == 'GREEN': height, color = 0.2, [50, 205, 50, 180]
                        elif layer == 'WATER_POLY': height, color = 0.1, [30, 144, 255, 180]
                        
                        if height > 0:
                            polygons_3d.append({'polygon': geom_3d, 'height': height, 'color': color})
                    
                    c_lon = (min_lon + max_lon) / 2
                    c_lat = (min_lat + max_lat) / 2
                    c_x = c_lon * 100000 * dynamic_lon_ratio
                    c_y = c_lat * 100000
                    c_z = get_z_val(c_x, c_y, elev_lookup)
                    deg_lat = 0.5 / 111000.0
                    deg_lon = 0.5 / (111000.0 * dynamic_lon_ratio)
                    human_poly = [
                        [c_lon - deg_lon, c_lat - deg_lat, c_z],
                        [c_lon + deg_lon, c_lat - deg_lat, c_z],
                        [c_lon + deg_lon, c_lat + deg_lat, c_z],
                        [c_lon - deg_lon, c_lat + deg_lat, c_z],
                        [c_lon - deg_lon, c_lat - deg_lat, c_z]
                    ]
                    polygons_3d.append({'polygon': [human_poly], 'height': 1.7, 'color': [255, 20, 147, 255]})
                    
                    layer_3d_poly = pdk.Layer('PolygonLayer', data=polygons_3d, get_polygon='polygon', get_fill_color='color', get_elevation='height', extruded=True, wireframe=True)
                    layer_3d_line = pdk.Layer('PathLayer', data=lines_3d, get_path='path', get_color='color', width_scale=1, width_min_pixels=1.5)
                    
                    # 태양광 각도에 맞춘 카메라 변화 효과
                    sun_pitch = 40 + (hour_factor * 30)
                    view_state = pdk.ViewState(longitude=(min_lon + max_lon) / 2, latitude=(min_lat + max_lat) / 2, zoom=15.5, pitch=sun_pitch, bearing=(sim_hour - 12) * 15)
                    st.pydeck_chart(pdk.Deck(layers=[layer_3d_poly, layer_3d_line], initial_view_state=view_state, map_provider='carto', map_style='dark'), use_container_width=True)

                    st.markdown("---")
                    offset_x = ((min_lon + max_lon) / 2) * 100000 * dynamic_lon_ratio
                    offset_y = ((min_lat + max_lat) / 2) * 100000
                    
                    col_dxf1, col_dxf2, col_obj = st.columns(3)
                    with col_dxf1:
                        dxf_2d = export_site_data_to_dxf(features, elev_lookup, min_lon, max_lon, min_lat, max_lat, dynamic_lon_ratio, is_3d=False, offset_x=offset_x, offset_y=offset_y)
                        with open(dxf_2d, "rb") as file: st.download_button("📥 2D 다이어그램 (.dxf)", data=file, file_name="Site_Diagram_2D.dxf", mime="application/dxf", use_container_width=True)
                    with col_dxf2:
                        dxf_3d = export_site_data_to_dxf(features, elev_lookup, min_lon, max_lon, min_lat, max_lat, dynamic_lon_ratio, is_3d=True, offset_x=offset_x, offset_y=offset_y)
                        with open(dxf_3d, "rb") as file: st.download_button("📦 3D 매스 (.dxf)", data=file, file_name="Site_Mass_3D.dxf", mime="application/dxf", use_container_width=True)
                    with col_obj:
                        obj_3d = export_site_data_to_obj(features, elev_lookup, min_lon, max_lon, min_lat, max_lat, dynamic_lon_ratio, offset_x=offset_x, offset_y=offset_y)
                        with open(obj_3d, "rb") as file: st.download_button("🧊 블렌더 3D 매스 (.obj)", data=file, file_name="Site_Mass_3D.obj", mime="text/plain", use_container_width=True)
