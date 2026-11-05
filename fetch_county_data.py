"""Download the build inputs directly from LA County's public ArcGIS services.

Usage:
  python fetch_county_data.py --out work                       # whole county (~2.4M parcels; takes a while)
  python fetch_county_data.py --out work --city PASADENA       # one city, for testing

Writes:
  work/roll.csv                  Assessor roll (the fields build_county.py uses), from the "Assessor Parcel Data Roll" service
  work/parcels.gpkg              Parcel polygons with AIN in EPSG:2229, from the Assessor parcels service
  work/la_cpa.geojson            City of LA Community Plan Areas (LA City Planning), EPSG:2229
  work/unincorporated.geojson    Official unincorporated community boundaries (County CSAs), EPSG:2229

When the County publishes a new roll year, update ROLL_YEAR and the two service URLs below.
Uses only the Python standard library plus numpy/shapely/pyogrio (already required by build_county.py).
"""
import argparse, csv, json, os, ssl, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pyogrio, shapely

ROLL_YEAR = '2026'
ROLL_URL = 'https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/Assessor%20Parcel%20Data%20Roll%202026/FeatureServer/0'
PARCELS_URL = 'https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/ASSR_PARCELS_25_View/FeatureServer/0'
CPA_URL = 'https://services5.arcgis.com/7nsPwEMP38bSkCjy/ArcGIS/rest/services/Community_Plan_Areas/FeatureServer/0'
CSA_URL = 'https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/Countywide_Statistical_Areas_with_348_polygons/FeatureServer/0'
# roll fields used by build_county.py (same names as the County's CSV export)
ROLL_FIELDS = ['AIN', 'TaxRateArea_CITY', 'TaxRateArea', 'YearBuilt', 'Roll_LandBaseYear', 'netTaxableValue', 'SQFTmain',
               'Roll_TotalValue', 'UseType', 'UseCodeDescChar1', 'UseCodeDescChar3', 'Units', 'PropertyLocation', 'SitusZIP5',
               'SitusHouseNo', 'SitusDirection', 'SitusStreet', 'SitusCity', 'CENTER_LON', 'CENTER_LAT']
WORKERS = 6        # parallel requests; the County's servers throttle heavy use
PAGE = 1000        # both services return at most 1000 records per request

def log(*a): print(*a, flush=True)
try:    # python.org's macOS Python doesn't use the system certificates; certifi's bundle works everywhere
    import certifi; SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL = ssl.create_default_context()

def get(url, params, tries=6):
    data = urllib.parse.urlencode(params).encode()
    for t in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers={'User-Agent': 'la-county-parcel-3d-build'}), timeout=120, context=SSL) as r:
                d = json.load(r)
            if 'error' in d: raise RuntimeError(d['error'])
            return d
        except Exception as e:
            if t == tries - 1: raise
            wait = 5 * 2 ** t; log(f'  retry in {wait}s: {e}'); time.sleep(wait)

def id_ranges(url, where):
    """Split the layer into object-ID slices of PAGE ids so pages can be fetched in parallel.
    (Don't use returnIdsOnly: the County's roll service caps that list at 1,000,000 ids.)"""
    oid = get(url, {'f': 'json'})['objectIdField']
    n = get(url + '/query', {'where': where, 'returnCountOnly': 'true', 'f': 'json'})['count']
    st = [{'statisticType': t, 'onStatisticField': oid, 'outStatisticFieldName': t} for t in ('min', 'max')]
    a = get(url + '/query', {'where': where, 'outStatistics': json.dumps(st), 'f': 'json'})['features'][0]['attributes']
    lo, hi = int(a['min']), int(a['max'])
    return oid, [(i, min(i + PAGE - 1, hi)) for i in range(lo, hi + 1, PAGE)], n

def fetch_all(url, where, out_fields, geometry, label):
    oid, ranges, n = id_ranges(url, where)
    log(f'{label}: {n:,} records in {len(ranges):,} requests')
    def page(r):
        p = {'where': f'({where}) AND {oid} >= {r[0]} AND {oid} <= {r[1]}', 'outFields': ','.join(out_fields), 'f': 'json',
             'returnGeometry': 'true' if geometry else 'false'}
        if geometry: p['outSR'] = '2229'
        d = get(url + '/query', p)
        if d.get('exceededTransferLimit'): raise RuntimeError(f'{label}: more than {PAGE} records in ids {r}')
        return d['features']
    feats, done, t0 = [], 0, time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        for fs in ex.map(page, ranges):
            feats.extend(fs); done += 1
            if done % 200 == 0 or done == len(ranges): log(f'  {done:,}/{len(ranges):,} requests, {len(feats):,} records, {time.time() - t0:.0f}s')
    if len(feats) != n: sys.exit(f'{label}: the County reports {n:,} records but {len(feats):,} were fetched; stopping so a partial map is not published')
    return feats

def esri_polygon(rings):
    """Esri JSON rings -> shapely (Multi)Polygon. Outer rings are clockwise, holes counter-clockwise."""
    outers, holes = [], []
    for r in rings:
        if len(r) < 4: continue
        a = np.asarray(r, dtype=float); area = 0.5 * np.sum(a[:-1, 0] * a[1:, 1] - a[1:, 0] * a[:-1, 1])
        (outers if area < 0 else holes).append(r)
    if not outers: return None
    polys = [[o, []] for o in outers]
    for h in holes:
        pt = shapely.Point(h[0])
        host = next((p for p in polys if shapely.Polygon(p[0]).contains(pt)), polys[0])
        host[1].append(h)
    ps = [shapely.Polygon(o, hs) for o, hs in polys]
    return ps[0] if len(ps) == 1 else shapely.MultiPolygon(ps)

def write_areas(url, where, name_field, path, label):
    """Boundary polygons -> GeoJSON in EPSG:2229 with a 'name' property (what build_county.py reads)."""
    feats = fetch_all(url, where, [name_field], True, label)
    out = [{'type': 'Feature', 'properties': {'name': (f['attributes'][name_field] or '').strip()},
            'geometry': json.loads(shapely.to_geojson(esri_polygon(f['geometry']['rings'])))} for f in feats if f.get('geometry')]
    json.dump({'type': 'FeatureCollection', 'features': out}, open(path, 'w'))
    log(f'wrote {os.path.basename(path)} ({len(out)} areas)')

ap = argparse.ArgumentParser(); ap.add_argument('--out', default='work'); ap.add_argument('--city', help="e.g. PASADENA (testing)")
A = ap.parse_args(); os.makedirs(A.out, exist_ok=True)

# 0. area boundaries used to split the biggest jurisdictions into pages
write_areas(CPA_URL, '1=1', 'NAME_ALF', os.path.join(A.out, 'la_cpa.geojson'), 'LA Community Plan Areas')
write_areas(CSA_URL, "CITY_TYPE = 'Unincorporated'", 'COMMUNITY', os.path.join(A.out, 'unincorporated.geojson'), 'unincorporated communities')

# 1. assessor roll -> CSV
where = f"RollYear = '{ROLL_YEAR}'" + (f" AND TaxRateArea_CITY LIKE '{A.city.upper()}%'" if A.city else '')
rows = fetch_all(ROLL_URL, where, ROLL_FIELDS, False, 'assessor roll')
with open(os.path.join(A.out, 'roll.csv'), 'w', newline='') as f:
    w = csv.writer(f); w.writerow(ROLL_FIELDS)
    for r in rows: a = r['attributes']; w.writerow(['' if a.get(k) is None else a[k] for k in ROLL_FIELDS])
ains = {str(r['attributes']['AIN']).strip() for r in rows}
log(f'wrote roll.csv ({len(rows):,} parcels)')

# 2. parcel polygons -> GeoPackage (EPSG:2229). For --city, fetch the city's bounding box and keep its AINs.
pwhere = '1=1'
if A.city:
    lon = [float(r['attributes']['CENTER_LON']) for r in rows if r['attributes'].get('CENTER_LON')]
    lat = [float(r['attributes']['CENTER_LAT']) for r in rows if r['attributes'].get('CENTER_LAT')]
    pwhere = f'CENTER_LON >= {min(lon) - .01} AND CENTER_LON <= {max(lon) + .01} AND CENTER_LAT >= {min(lat) - .01} AND CENTER_LAT <= {max(lat) + .01}'
feats = fetch_all(PARCELS_URL, pwhere, ['AIN'], True, 'parcel polygons')
geoms, out_ains = [], []
for f in feats:
    a = str(f['attributes'].get('AIN') or '').strip(); g = f.get('geometry')
    if not g or (A.city and a not in ains): continue
    p = esri_polygon(g['rings'])
    if p is not None: geoms.append(p); out_ains.append(a)
path = os.path.join(A.out, 'parcels.gpkg')
if os.path.exists(path): os.remove(path)
pyogrio.raw.write(path, np.array(shapely.to_wkb(geoms), dtype=object), [np.array(out_ains, dtype=object)], ['AIN'],
                  driver='GPKG', crs='EPSG:2229', geometry_type='Unknown', layer='parcels')
log(f'wrote parcels.gpkg ({len(geoms):,} polygons)')
