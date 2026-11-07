"""Build the LA County property-tax revenue maps.

Usage (see README for where each input comes from):
  python build_county.py --roll work/roll.csv --parcels work/parcels.gpkg --out site

Inputs:
  --roll      LA County Assessor roll as CSV (fetch_county_data.py writes this from the County's service)
  --parcels   parcel polygons with an AIN field, in EPSG:2229 (any format GDAL reads: .gpkg, .gdb, .shp)
  --af49      Auditor-Controller AF49 "Modified ATI Ratios" report (.txt or .txt.gz)   [default: data/]
  --cr12      Auditor-Controller CR12 "Secured Tax Rates and Ratios" report (.txt or .txt.gz)   [default: data/]
  --cpa       LA City Community Plan Areas GeoJSON in EPSG:2229   [default: data/]
  --uninc     official unincorporated community boundaries (County CSAs) GeoJSON in EPSG:2229   [default: data/]
Output (--out):
  index.html, LA_County_Overview.html   250 m cells over the whole county; click a cell to open its parcel page
  pages/*.html                          parcel footprints per city; Los Angeles per Community Plan Area; unincorporated per community
  search/                               address/APN index and location lookup, sharded into small script files

Revenue per assessor parcel = net taxable value x 1% x the share of that levy going to the parcel's own city
in its tax rate area (all of the city's AF49 accounts), or to the LA County general fund (001.05) when unincorporated.
Stacked polygons with identical shapes (condo units) are merged into one footprint and their revenue summed.
Requires pyogrio, shapely, pyproj, numpy (requirements.txt).
"""
import argparse, base64, csv, gzip, json, math, os, re, sys, statistics
from collections import Counter, defaultdict
import numpy as np, pyogrio, shapely
from pyproj import Transformer

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
ap.add_argument('--roll', required=True); ap.add_argument('--parcels', required=True); ap.add_argument('--parcels-layer')
ap.add_argument('--af49', default=os.path.join(HERE, 'data', 'AF49PRPT.YR26.txt.gz'))
ap.add_argument('--cr12', default=os.path.join(HERE, 'data', 'CR12_Secured_Tax_Rates_FY2026-27.txt.gz'))
ap.add_argument('--cpa', default=os.path.join(HERE, 'data', 'LA_Community_Plan_Areas_EPSG2229.geojson'))
ap.add_argument('--osm', help='OpenStreetMap parks and parking (osm_places.geojson from fetch_county_data.py), EPSG:2229; optional')
ap.add_argument('--uninc', default=os.path.join(HERE, 'data', 'LA_County_Unincorporated_Communities_EPSG2229.geojson'))
ap.add_argument('--out', default='site')
A = ap.parse_args()
AF49, CPA, CR12, OUT = A.af49, A.cpa, A.cr12, A.out
def text(path): return gzip.open(path, 'rt', encoding='latin-1') if path.endswith('.gz') else open(path, encoding='latin-1')
for d in ('pages', 'search/apn', 'search/addr', 'search/cells'): os.makedirs(os.path.join(OUT, d), exist_ok=True)
LEVY = 0.01
BUILT, BASE = (1880, 2025), (1975, 2026)   # must match DIMS lo/hi in the template
FT = 0.3048006                             # EPSG:2229 is in US survey feet
CELL_FT = 250 / FT                         # overview grid cell
COUNTY_GF = '001.05'
LA, UNINC = 'LOS ANGELES', 'UNINCORPORATED'
TPL = open(os.path.join(HERE, 'county_revenue_template.html')).read()
TO_LL = Transformer.from_crs(2229, 4326, always_xy=True)

def log(*a): print(*a, flush=True)
# CR12: total secured tax rate (%) per TRA = 1% general levy + voter-approved debt (bond) rates
TOTAL_RATE = {}
_cr = text(CR12).read().splitlines()
for _i, _l in enumerate(_cr):
    _m = re.search(r'RATES\s+TYPE 1.*TRA\s+(\d{5})', _l)
    if _m: TOTAL_RATE[_m.group(1)] = float(_cr[_i + 1].split()[5])
del _cr
def num(x):
    try: return float(x)
    except ValueError: return 0.0
def mode(xs): return Counter(xs).most_common(1)[0][0] if xs else 0
def zip5(z):
    z = (z or '').strip()[:5]
    return z if re.fullmatch(r'\d{5}', z) else ''
CATS = ['Single-family', 'Condo', 'Multifamily', 'Commercial', 'Industrial', 'Institutional & other', 'Vacant', 'Mixed-use',
        'Parking', 'Park & open space']   # must match CATS in the template
RES, CI, MIXED, PARKING, PARK = {0, 1, 2}, {3, 4}, 7, 8, 9
# assessor use codes that combine commercial and residential on one parcel
MIXED_CODES = ('Store and Residential', 'Office and Residential', 'Artist in Residence', 'Apartment Combination')
def category(use, desc, desc2, desc3):
    if use == 'C/I' and any(m in desc3 for m in MIXED_CODES): return MIXED
    if desc2.startswith('Parking Lot'): return PARKING   # incl. 'Parking Structures - Commercial Parking'
    return {'SFR': 0, 'CND': 1, 'R-I': 2, 'OTH': 5, 'VAC': 6}.get(use, 4 if desc == 'Industrial' else 3) if use else 5
def footprint_category(cats):
    # mixed-use: a combination use code, or residential and commercial/industrial parcels sharing one footprint
    if MIXED in cats or (RES & set(cats) and CI & set(cats)): return MIXED
    return mode(cats)
def slug(s): return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')
def situs_address(r):
    """'115 W PARK CIR #PH42' from the roll's situs fields (PropertyLocation pads units with runs of spaces)."""
    house = (r.get('SitusHouseNo') or '').strip()
    street = re.sub(r'\s+', ' ', ' '.join(x for x in ((r.get('SitusDirection') or '').strip(), (r.get('SitusStreet') or '').strip()) if x))
    if not (house and house != '0' and street): return r['PropertyLocation'].split('  ')[0].strip()
    unit = re.sub(r'\s+', '', re.sub(r'^(NO|UNIT|APT|STE|#)\s*', '', (r.get('SitusUnit') or '').strip()))
    return f'{house} {street}' + (f' #{unit}' if unit else '')
# short labels for the "what's on this footprint" tooltip line
SHORT_USE = {'Hotel and Motel': 'hotel', 'Office Building': 'office', 'Store': 'store', 'Store Combination': 'store',
             'Restaurant, Cocktail Lounge': 'restaurant', 'Parking Lot (Commercial Use Property)': 'parking',
             'Shopping Center (Neighborhood, Community)': 'shopping center', 'Government Owned Property': 'government',
             'Professional Building': 'professional office', 'Light Manufacturing': 'light industrial'}
def kind(use, desc2):
    return {'CND': 'condo', 'SFR': 'home', 'R-I': 'apartment', 'VAC': 'vacant'}.get(use) or SHORT_USE.get(desc2) or (desc2.lower() or 'other')
def plural(n, k):
    return f"{n} {k}{'s' if n > 1 else ''}" if k in ('condo', 'home') else f"{n} {k} parcel{'s' if n > 1 else ''}"
def money(v): return f'${v / 1e6:.1f}M' if v >= 1e6 else f'${v / 1e3:.0f}k' if v >= 1e3 else f'${v:,.0f}'
def stacked_kind(r):
    # parcels stacked on one footprint are air-space units: single-family ones are condos whose use code carries another
    # flag in place of the condo 'C' (e.g. 010G = Mills Act historic property, as at Castle Green)
    return 'condo' if r[6] == 'SFR' else kind(r[6], r[13])
def breakdown(rs):
    """e.g. '331 condos + 48 hotel parcels · largest: $317.2M hotel, $282.3M hotel, $53.1M condo PH42'"""
    kinds = Counter(stacked_kind(r) for r in rs).most_common()
    parts = [plural(n, k) for k, n in kinds[:3]] + ([f'{sum(n for k, n in kinds[3:])} other'] if len(kinds) > 3 else [])
    def unit(addr): return addr[addr.index(' #') + 1:] if ' #' in addr else ''
    top = sorted(rs, key=lambda r: -r[4])[:3]
    return ' + '.join(parts) + ' · largest: ' + ', '.join(f"{money(r[4])} {stacked_kind(r)}{(' ' + unit(r[7])) if unit(r[7]) else ''}" for r in top)
def footprint_address(rs, lead):
    """Single parcel: its own address. Several: every distinct street address on the footprint (without unit numbers),
    most common first, up to three. A development's parcels can carry different addresses (e.g. Century Plaza's towers
    are on the roll as W Elm Ct and W Park Cir as well as Avenue of the Stars), and the roll doesn't say which is the
    public street, so show them all rather than guess."""
    if len(rs) == 1: return lead[7]
    bases = Counter(r[7].split(' #')[0].split(',')[0].strip() for r in rs if r[7]).most_common()
    if not bases: return ''
    shown = ' / '.join(b for b, n in bases[:3])
    return shown + (f' + {len(bases) - 3} more' if len(bases) > 3 else '')
def to_ll(gs): return shapely.transform(gs, lambda xy: np.column_stack(TO_LL.transform(xy[:, 0], xy[:, 1])))

# ---------- 1. AF49: agency ratios per tax rate area
ratios = defaultdict(list)
tra = None
for ln in text(AF49):
    m = re.match(r'\s+(\d{5})\s+(\d{3}\.\d{2})\s+(.+?)\s+(\.\d+)\s*$', ln)
    if m: tra = m.group(1); ratios[tra].append((m.group(2), m.group(3), float(m.group(4)))); continue
    m = re.match(r'\s{10,}(\d{3}\.\d{2})\s+(.+?)\s+(\.\d+)\s*$', ln)
    if m and tra: ratios[tra].append((m.group(1), m.group(2), float(m.group(3))))
city_prefixes = {t: Counter(a[:3] for a, n, v in rs if re.match(r'CITY[\s-]', n)) for t, rs in ratios.items()}
log('AF49 TRAs', len(ratios))

# ---------- 2. assessor records
raw = []
votes = defaultdict(Counter)   # city -> which AF49 account prefix is this city's
with open(A.roll, newline='', encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        city = r['TaxRateArea_CITY'].strip().upper(); t = r['TaxRateArea'].strip()
        if city != UNINC:
            for p in city_prefixes.get(t, ()): votes[city][p] += 1
        yb = int(num(r['YearBuilt'])); by = int(num(r['Roll_LandBaseYear']))
        raw.append((r['AIN'].strip(), city, t, num(r['netTaxableValue']), yb, max(1975, by) if by else 0,
                    int(num(r['SQFTmain'])), int(num(r['Roll_TotalValue'])), r['UseType'].strip(),
                    situs_address(r), zip5(r['SitusZIP5']),
                    r['SitusHouseNo'].strip(), r['SitusDirection'].strip().upper(), re.sub(r'\s+', ' ', r['SitusStreet'].strip().upper()),
                    re.sub(r'\s+CA$', '', r['SitusCity'].strip().upper()), num(r['CENTER_LON']), num(r['CENTER_LAT']),
                    int(num(r['Units'])), category(r['UseType'].strip(), r['UseCodeDescChar1'].strip(), r.get('UseCodeDescChar2', '').strip(), r['UseCodeDescChar3'].strip()),
                    r.get('UseCodeDescChar2', '').strip(), r['UseCodeDescChar3'].strip()))
prefix = {c: v.most_common(1)[0][0] for c, v in votes.items()}
share_cache = {}
def share(city, t):
    k = (city, t)
    if k not in share_cache:
        rs = ratios.get(t, [])
        share_cache[k] = (sum(v for a, n, v in rs if a == COUNTY_GF) if city == UNINC else
                          sum(v for a, n, v in rs if a[:3] == prefix.get(city) and re.match(r'CITY[\s-]', n)))
    return share_cache[k]
recs = {}    # AIN -> (rev, yb, base, sqft, assessed, net, use, addr, city, zip, units, category, est. total tax, use description, use detail)
SEARCH = {}  # AIN -> (house, dir, street, postal city, zip, lon, lat)
for x in raw:
    ain, city, t, net = x[0], x[1], x[2], x[3]
    recs[ain] = (round(net * LEVY * share(city, t)), x[4], x[5], x[6], x[7], int(net), x[8], x[9], city, x[10], x[17], x[18],
                 round(net * TOTAL_RATE.get(t, 100 * LEVY) / 100), x[19], x[20])
    SEARCH[ain] = (x[11], x[12], x[13], x[14], x[10], x[15], x[16])
del raw
log('records', len(recs), '| county revenue $%.2fB' % (sum(r[0] for r in recs.values()) / 1e9),
    '| est. owner tax (1%% + bonds) $%.2fB' % (sum(r[12] for r in recs.values()) / 1e9), '| TRAs w/o CR12 rate:', len({x for x in share_cache if x[1] not in TOTAL_RATE}))

# ---------- 3. parcel polygons -> footprints (identical shapes merged)
# TaxRateCity/SitusZIP are optional (the County's local geodatabase has them; its online parcel service doesn't).
# They're only used to place polygons that have no assessor record.
avail = set(pyogrio.read_info(A.parcels, layer=A.parcels_layer)['fields'])
meta, _, geom, fields = pyogrio.raw.read(A.parcels, layer=A.parcels_layer, columns=[c for c in ('AIN', 'TaxRateCity', 'SitusZIP') if c in avail])
col = {n: i for i, n in enumerate(meta['fields'])}   # pyogrio returns columns in layer order, not request order
ok = np.array([g is not None for g in geom])
ains = np.array([str(a).strip() for a in fields[col['AIN']][ok]], dtype=object)
blank = np.full(int(ok.sum()), '', dtype=object)
gcity = fields[col['TaxRateCity']][ok] if 'TaxRateCity' in col else blank; gzip_ = fields[col['SitusZIP']][ok] if 'SitusZIP' in col else blank
geoms = shapely.from_wkb(geom[ok]); del geom
keys = shapely.to_wkb(shapely.normalize(geoms))
gid = {}; member = []
for i, k in enumerate(keys):
    g = gid.get(k)
    if g is None: g = gid[k] = len(member); member.append([])
    member[g].append(i)
del keys, gid
log('polygons', len(geoms), '-> footprints', len(member))

# the polygon layer spells some cities differently (WEST LAKE VILLAGE, LA CANADA-FLINTRIDGE): map onto assessor names
canon = {re.sub(r'[^A-Z]', '', c): c for c in {r[8] for r in recs.values()}}
def gdb_city(c):
    c = (c or '').strip().upper() or UNINC
    return canon.get(re.sub(r'[^A-Z]', '', c), c)
if 'TaxRateCity' not in col:   # no way to place polygons without an assessor record ($0 anyway): leave them out
    member = [idx for idx in member if any(ains[i] in recs for i in idx)]
FPS = []   # [rev, yb, n, addr, label, sqft, geomIndex, assessed, net, base, city, zip, units, category, est. total tax, breakdown]
FP_RS = []  # the assessor records on each footprint (for merging parks)
def fp_row(rs, idx0, gi=None):
    lead = max(rs, key=lambda r: r[5])
    cats = [r[11] for r in rs]
    if len(rs) > 1: cats = [1 if c == 0 else c for c in cats]   # stacked single-family parcels are condos (see stacked_kind)
    cat = footprint_category(cats)
    label = ''
    if cat == PARKING:
        label = 'Parking structure' if any('Structure' in r[14] for r in rs if r[11] == PARKING) else 'Parking lot'
    return [sum(r[0] for r in rs), mode([r[1] for r in rs if r[1]]), len(rs), footprint_address(rs, lead), label,
            sum(r[3] for r in rs), idx0 if gi is None else gi, sum(r[4] for r in rs), sum(r[5] for r in rs),
            mode([r[2] for r in rs if r[2]]), mode([r[8] for r in rs]), mode([r[9] for r in rs if r[9]]) or 'other',
            sum(r[10] for r in rs if r[11] in RES or r[11] == MIXED), cat, sum(r[12] for r in rs),
            breakdown(rs) if len(rs) > 1 else '']
for idx in member:
    rs = [recs[ains[i]] for i in idx if ains[i] in recs]
    FP_RS.append(rs)
    if rs: FPS.append(fp_row(rs, idx[0]))
    else:
        FPS.append([0, 0, 0, '', '', 0, idx[0], 0, 0, 0, gdb_city(gcity[idx[0]]), zip5(gzip_[idx[0]]) or 'other', 0, 5, 0, ''])

# ---------- 3b. parks and public parking from OpenStreetMap
# The roll only says "Government Owned Property" (or nothing) for public land. A government, institutional or vacant
# footprint at least half covered by an OpenStreetMap park becomes "Park & open space"; one half covered by parking
# becomes "Parking". Footprints in the same park and city are merged into one, and the park's area that isn't on any
# other parcel (paths and closed streets through the park) is filled in.
if A.osm:
    feats = json.load(open(A.osm))['features']
    og = np.array([shapely.geometry.shape(f['geometry']) for f in feats], dtype=object); op = [f['properties'] for f in feats]
    otree = shapely.STRtree(og)
    cand = np.array([k for k, f in enumerate(FPS) if f[13] in (5, 6)])
    fg = geoms[[FPS[k][6] for k in cand]]
    pi, oi = otree.query(fg, predicate='intersects')
    share = shapely.area(shapely.intersection(fg[pi], og[oi])) / np.maximum(shapely.area(fg[pi]), 1)
    cover = defaultdict(lambda: defaultdict(float)); best = {}
    for p_, o, sh in zip(pi, oi, share):
        kd = op[o]['kind']; cover[p_][kd] += sh
        if sh > best.get((p_, kd), (0, -1))[0]: best[(p_, kd)] = (sh, o)
    park_groups = defaultdict(list); n_park = n_parking = 0
    for p_, cv in cover.items():
        k = int(cand[p_]); f = FPS[k]; rs = FP_RS[k]
        public = any(r[13] == 'Government Owned Property' for r in rs)
        if cv['park'] >= 0.5:
            o = best[(p_, 'park')][1]; f[13] = PARK; n_park += 1
            f[4] = 'Park & open space' + (f" · {op[o]['name']}" if op[o]['name'] else '')
            if not f[3] and op[o]['name']: f[3] = op[o]['name']
            if rs: park_groups[(f[10], o)].append(k)
        elif cv['parking'] >= 0.5:
            o = best[(p_, 'parking')][1]; f[13] = PARKING; n_parking += 1
            garage = op[o]['parking'] in ('multi-storey', 'underground', 'rooftop')
            f[4] = ('Parking garage' if garage else 'Parking lot') + (' (publicly owned)' if public else '') + (f" · {op[o]['name']}" if op[o]['name'] else '')
            if not f[3] and op[o]['name']: f[3] = op[o]['name']
    log(f'OpenStreetMap: {len(og):,} parks/parking areas | {n_park:,} footprints marked park, {n_parking:,} parking')
    # merge each park's footprints (per city) into one, filling unparceled gaps inside the park
    ptree = shapely.STRtree(geoms); drop = set(); new_geoms = []
    for (city, o), ks in park_groups.items():
        mine = {i for k in ks for i in member[k]}
        members = shapely.union_all(geoms[[FPS[k][6] for k in ks]])
        others = [i for i in ptree.query(og[o], predicate='intersects') if i not in mine]
        gap = shapely.difference(og[o], shapely.union_all(geoms[others])) if others else og[o]
        gap = shapely.buffer(shapely.buffer(gap, -5), 5)      # drop slivers along the park's edge (< 10 ft wide)
        # keep gaps mostly bordered by the park's own parcels (a path or closed street through the park), not roads
        # running between private parcels inside a large reserve
        near = shapely.buffer(members, 3)
        parts = [g for g in getattr(gap, 'geoms', [gap]) if g.geom_type == 'Polygon' and g.area > 500
                 and g.boundary.intersection(near).length >= 0.5 * g.boundary.length]
        geom = shapely.union_all([members] + parts) if parts else members
        if len(ks) == 1 and not parts: continue
        rs = [r for k in ks for r in FP_RS[k]]
        row = fp_row(rs, None, len(geoms) + len(new_geoms)); new_geoms.append(shapely.make_valid(geom))
        row[13], row[4] = PARK, FPS[ks[0]][4]
        row[3] = (FPS[ks[0]][4].split(' · ', 1)[1] if ' · ' in FPS[ks[0]][4] else '') or row[3]
        row[15] = f'{len(rs)} parcels' + (f' · {money(row[7])} assessed in all' if row[7] else ' · all exempt / $0') if len(rs) > 1 else ''
        FPS.append(row); FP_RS.append(rs); member.append(sorted(mine)); drop.update(ks)
    if new_geoms: geoms = np.concatenate([geoms, np.array(new_geoms, dtype=object)])
    keep = [k for k in range(len(FPS)) if k not in drop]
    FPS = [FPS[k] for k in keep]; member = [member[k] for k in keep]; FP_RS = [FP_RS[k] for k in keep]
    log(f'  parks: {len(drop):,} footprints merged into {len(new_geoms):,}')
del FP_RS
VMAX = float(np.quantile([f[0] for f in FPS if f[0] > 0], 0.999))
_top = max(FPS, key=lambda f: f[0]); log('height reference (99.9th pct) $%d/yr | largest footprint $%d/yr = %.0fx reference (%s)' % (VMAX, _top[0], _top[0] / VMAX, _top[3]))
cent = shapely.centroid(geoms[[f[6] for f in FPS]])

# ---------- 4. page assignment: LA by Community Plan Area, unincorporated by official community, other cities whole
def load_areas(path):
    fs = json.load(open(path))['features']
    names = [(f['properties'].get('name') or f['properties'].get('NAME_ALF') or '').strip() for f in fs]
    geoms_ = np.array([shapely.geometry.shape(f['geometry']) for f in fs])
    return geoms_, names, shapely.STRtree(geoms_)
def area_names(points, areas):
    """Name of the area containing each point (nearest area for points just outside every boundary)."""
    _, names, tree = areas; out = np.full(len(points), -1)
    if len(points):
        pi, gi = tree.query(points, predicate='within'); out[pi] = gi
        miss = np.where(out < 0)[0]
        if len(miss): out[miss] = tree.query_nearest(points[miss], return_distance=False, all_matches=False)[1]
    return [names[i] for i in out]
CPA_AREAS, UNINC_AREAS = load_areas(CPA), load_areas(A.uninc)
for city, areas in ((LA, CPA_AREAS), (UNINC, UNINC_AREAS)):
    idx = [i for i, f in enumerate(FPS) if f[10] == city]
    for i, n in zip(idx, area_names(cent[idx], areas)): FPS[i][11] = n
log('LA Community Plan Areas:', len(CPA_AREAS[1]), '| unincorporated communities:', len(UNINC_AREAS[1]))
def page_key(city, sub): return (city, sub if city in (LA, UNINC) else None)
def nice(c): return 'Unincorporated LA County' if c == UNINC else c.title().replace("'S", "'s")
pages = defaultdict(list)
for i, f in enumerate(FPS): pages[page_key(f[10], f[11])].append(i)

def ring(c, lon0, lat0):
    """Ring as [x0, y0, dx1, dy1, ...] in 1e-6 degrees: first corner from the page origin, then each step from the previous
    corner (small numbers keep the pages compact; the template adds them back up)."""
    c = np.asarray(c)[:-1]
    xy = np.column_stack(((c[:, 0] - lon0) * 1e6, (c[:, 1] - lat0) * 1e6)).round().astype(np.int64)
    return np.vstack([xy[:1], np.diff(xy, axis=0)]).ravel().tolist()
def polys_of(g, lon0, lat0):
    out = []
    for p in (g.geoms if g.geom_type == 'MultiPolygon' else [g]):
        if p.is_empty or p.geom_type != 'Polygon' or len(p.exterior.coords) < 4: continue
        out.append([ring(p.exterior.coords, lon0, lat0)] + [ring(h.coords, lon0, lat0) for h in p.interiors])
    return out
def packed(obj):
    """JSON -> gzip -> base64 string (the template unzips it in the browser); keeps the published site ~3x smaller."""
    return json.dumps(base64.b64encode(gzip.compress(json.dumps(obj, separators=(',', ':')).encode(), 9)).decode())
def write(path, rows, meta, agg=None):
    html = TPL.replace('/*DATA*/[]', packed(rows)).replace('/*META*/{}', json.dumps(meta)).replace('/*AGG*/null', packed(agg) if agg else 'null')
    open(path, 'w').write(html); return len(html)

page_list = []; page_no = {}; total_bytes = 0; written = set()
for (c, sub), idx in sorted(pages.items(), key=lambda kv: (kv[0][0], kv[0][1] or '')):
    gs = shapely.simplify(geoms[[FPS[i][6] for i in idx]], 1.0)
    keep = [k for k, g in enumerate(gs) if shapely.area(g) >= 5]
    idx = [idx[k] for k in keep]; gs = to_ll(gs[keep])
    b = shapely.total_bounds(gs); lon0, lat0 = round((b[0] + b[2]) / 2, 4), round((b[1] + b[3]) / 2, 4)
    rows = []
    for i, g in zip(idx, gs):
        f = FPS[i]; p = polys_of(g, lon0, lat0)
        if p: rows.append([f[0], f[1], f[2], f[3], f[4] or 0, f[5], p, f[7], f[8], f[9], 0, f[12], f[13], f[14], f[15]])   # [4] label (park, parking), [10] link: unused on parcel pages
    rev = sum(r[0] for r in rows); net = sum(r[8] for r in rows)
    sh = 100 * rev / (net * LEVY) if net else 0
    uninc = c == UNINC; name = nice(c)
    if uninc: where = f'unincorporated {sub}'
    else: where = f'the City of {name}' + (f' · {sub}' if sub else '')
    fn = slug(c) + ('-' + slug(sub) if sub else '') + '.html'; assert fn not in written, fn; written.add(fn)
    meta = {'title': f'{sub} Parcel Revenue' if c == LA else f'Unincorporated {sub} Parcel Revenue' if uninc else f'{name} Parcel Revenue',
            'kind': 'parcels', 'unit': 'footprints', 'vmax': VMAX, 'origin': [lon0, lat0], 'root': '../', 'self': 'pages/' + fn,
            'h1': f'Property tax to LA County · {where}' if uninc else f'Property tax to {where}',
            'hdesc': 'estimated annual property tax to the ' + ('County general fund' if uninc else 'City') + ' (all parcels on a footprint summed)',
            'revWord': 'County revenue' if uninc else 'City revenue',
            'fnote': f"Here that's ≈{sh:.1f}% of the 1% levy. Sales, hotel, utility and other {'County' if uninc else 'city'} taxes aren't included. Heights use one county-wide scale.",
            'back': '../LA_County_Overview.html'}
    total_bytes += write(os.path.join(OUT, 'pages', fn), rows, meta)
    page_no[(c, sub)] = len(page_list)
    page_list.append([sub if sub else name, 'pages/' + fn, ('Los Angeles (Community Plan Areas)' if c == LA else name) if sub else ''])
    if c == 'PASADENA': log('Pasadena check: $%.1fM from %d footprints | est. owner tax $%.1fM' % (rev / 1e6, len(rows), sum(r[13] for r in rows) / 1e6))
log('pages', len(page_list), '| %.0f MB' % (total_bytes / 1e6), '| LA areas:', sum(1 for c, s in pages if c == LA))

# ---------- 5. search index: APN -> location + page; street -> addresses (sharded)
ain_page = {}
for f, idx in zip(FPS, member):
    pn = page_no.get(page_key(f[10], f[11]))
    if pn is not None:
        for i in idx: ain_page[ains[i]] = pn
del member
# APN shards: [APN without the shard's 3-digit prefix, lon, lat offsets in 1e-6 degrees from SEARCH_LON0/LAT0, page]
# address shards: {street: [[house, direction, cityIndex, zip, APN], ...]}   (must match the template's search code)
SEARCH_LON0, SEARCH_LAT0 = -119.0, 33.0
apn = defaultdict(list); addr = defaultdict(dict); cities = {}; nopage = 0
unplaced = [a for a in SEARCH if a not in ain_page and SEARCH[a][5]]
if unplaced:   # parcels with no polygon: place by city (LA by Community Plan Area, unincorporated by community)
    pts = shapely.points(np.array([[SEARCH[a][5], SEARCH[a][6]] for a in unplaced]))
    pts = shapely.transform(pts, lambda xy: np.column_stack(Transformer.from_crs(4326, 2229, always_xy=True).transform(xy[:, 0], xy[:, 1])))
    for a, la_n, un_n in zip(unplaced, area_names(pts, CPA_AREAS), area_names(pts, UNINC_AREAS)):
        city = recs[a][8]; sub = la_n if city == LA else un_n
        pn = page_no.get(page_key(city, sub))
        if pn is not None: ain_page[a] = pn
for a, (house, d, street, pcity, z, lon, lat) in SEARCH.items():
    pn = ain_page.get(a)
    if pn is None or not lon: nopage += 1; continue
    apn[a[:3]].append([a[3:], round((lon - SEARCH_LON0) * 1e6), round((lat - SEARCH_LAT0) * 1e6), pn])
    key = re.sub(r'[^A-Z0-9]', '', street)[:2]
    if len(key) == 2 and house and house != '0':
        ci = cities.setdefault(pcity, len(cities))
        addr[key].setdefault(street, []).append([house, d, ci, z, a])
def shard(kind, k, data):
    open(os.path.join(OUT, 'search', kind, k + '.js'), 'w').write(f'__S("{kind}/{k}",' + packed(data) + ');')
for k, v in apn.items(): shard('apn', k, v)
for k, v in addr.items(): shard('addr', k, {st: sorted(rs, key=lambda r: (r[1], r[0])) for st, rs in sorted(v.items())})
open(os.path.join(OUT, 'search', 'meta.js'), 'w').write('__S("meta",' + packed({'pages': page_list, 'cities': sorted(cities, key=cities.get)}) + ');')
log('search: %d APNs in %d shards, %d addresses in %d shards, %d unplaced' % (sum(map(len, apn.values())), len(apn), sum(len(rs) for v in addr.values() for rs in v.values()), len(addr), nopage))

# ---------- 6. overview: 250 m cells + exact year-by-year totals
xs, ys = shapely.get_x(cent), shapely.get_y(cent)
NC = len(CATS)
# assessed-value bands for the value-range sliders (must match VEDGES in the template); band i = [VEDGES[i], VEDGES[i+1])
VEDGES = [0, 1e4, 2.5e4, 5e4, 1e5, 2.5e5, 5e5, 1e6, 2.5e6, 5e6, 1e7, 2.5e7, 5e7, 1e8, 2.5e8, 5e8, 1e9]
NB = len(VEDGES)
def vband(v):
    b = 0
    while b + 1 < NB and v >= VEDGES[b + 1]: b += 1
    return b
cells = defaultdict(lambda: {'rev': 0, 'net': 0, 'tax': 0, 'av': 0, 'n': 0, 'sf': 0, 'units': 0, 'combo': Counter(), 'yb': [], 'by': [], 'page': Counter(), 'city': Counter()})
# per year: [rev, n, sqft] for each category, flattened; unk: [rev, n] for each category
# per year: [rev, n, sqft] for each (category, value band) pair, index (c*NB+b); unk: [rev, n] per pair
agg = {k: {'rows': [[0] * (3 * NC * NB) for _ in range(d[1] - d[0] + 1)], 'unk': [0] * (2 * NC * NB)} for k, d in (('built', BUILT), ('base', BASE))}
for f, x, y in zip(FPS, xs, ys):
    c = cells[(int(x // CELL_FT), int(y // CELL_FT))]
    c['rev'] += f[0]; c['net'] += f[8]; c['tax'] += f[14]; c['n'] += 1; c['sf'] += f[5]; c['units'] += f[12]; c['av'] += f[7]; ct = f[13] * NB + vband(f[7]); c['combo'][ct] += f[0]
    if f[1]: c['yb'].append(f[1])
    if f[9]: c['by'].append(f[9])
    c['page'][page_key(f[10], f[11])] += f[0] + 1
    c['city'][(f[11] + ', Los Angeles') if f[10] == LA else f'Unincorporated {f[11]}' if f[10] == UNINC else nice(f[10])] += f[0] + 1
    for k, yi, (lo, hi) in (('built', 1, BUILT), ('base', 9, BASE)):
        yv = f[yi]
        if not yv: agg[k]['unk'][2 * ct] += f[0]; agg[k]['unk'][2 * ct + 1] += 1; continue
        r = agg[k]['rows'][min(max(yv, lo), hi) - lo]; r[3 * ct] += f[0]; r[3 * ct + 1] += 1; r[3 * ct + 2] += f[5]
agg['total'] = sum(f[0] for f in FPS)

# location -> page lookup, so a click outside a page's own parcels can open the neighboring page.
# ~250 m cells (1/400 degree), each mapped to the page with the most revenue there; sharded by 0.1-degree tile.
NAVC = 400
lons, lats = TO_LL.transform(xs, ys)
nav = defaultdict(Counter)
for f, lo, la in zip(FPS, lons, lats):
    pn = page_no.get(page_key(f[10], f[11]))
    if pn is not None: nav[(math.floor(lo * NAVC), math.floor(la * NAVC))][pn] += f[0] + 1
nav_shards = defaultdict(dict)
for (ix, iy), cnt in nav.items(): nav_shards[f'{ix // 40}_{iy // 40}'][f'{ix},{iy}'] = cnt.most_common(1)[0][0]
for k, v in nav_shards.items():
    open(os.path.join(OUT, 'search', 'cells', k + '.js'), 'w').write(f'__S("cells/{k}",' + packed(v) + ');')
log('navigation cells', len(nav), 'in', len(nav_shards), 'shards')
keys = list(cells)
sq = shapely.box([i * CELL_FT for i, j in keys], [j * CELL_FT for i, j in keys], [(i + 1) * CELL_FT for i, j in keys], [(j + 1) * CELL_FT for i, j in keys])
sq = to_ll(sq); b = shapely.total_bounds(sq); lon0, lat0 = round((b[0] + b[2]) / 2, 4), round((b[1] + b[3]) / 2, 4)
crow = []
for k, g in zip(keys, sq):
    c = cells[k]; top = c['city'].most_common(2)
    pn = page_no.get(c['page'].most_common(1)[0][0])
    crow.append([c['rev'], int(statistics.median(c['yb'])) if c['yb'] else 0, c['n'], top[0][0] + (f' (+ {top[1][0]})' if len(top) > 1 else ''), 'cell', c['sf'],
                 polys_of(g, lon0, lat0), c['av'], c['net'], int(statistics.median(c['by'])) if c['by'] else 0, page_list[pn][1] if pn is not None else '', c['units'], [x for k, v in sorted(c['combo'].items()) for x in (k, v)], c['tax']])
cvmax = float(np.quantile([r[0] for r in crow if r[0] > 0], 0.999))
meta = {'title': 'LA County Parcel Revenue', 'kind': 'grid', 'unit': '250 m cells', 'vmax': cvmax, 'hmax': 2500, 'origin': [lon0, lat0],
        'root': '', 'self': '', 'ticks': [1e4, 1e5, 1e6, 1e7],
        'h1': 'Property tax to LA County cities',
        'hdesc': "estimated annual property tax to each parcel's city (County general fund where unincorporated), summed per 250 m cell",
        'revWord': 'Revenue',
        'fnote': "The share varies by city and tax rate area. Sales, hotel, utility and other local taxes aren't included.",
        'pages': page_list}
ob = write(os.path.join(OUT, 'LA_County_Overview.html'), crow, meta, agg)
open(os.path.join(OUT, 'index.html'), 'w').write('<!doctype html><meta charset="utf-8"><title>LA County Parcel Revenue</title>'
    '<meta http-equiv="refresh" content="0; url=LA_County_Overview.html"><a href="LA_County_Overview.html">LA County parcel revenue map</a>')
log('overview cells', len(crow), '| %.1f MB' % (ob / 1e6), '| county total $%.2fB/yr' % (agg['total'] / 1e9))
