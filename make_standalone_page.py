"""Make a single-file version of one parcel page, with that page's search index embedded."""
import glob, json, os, re, sys
OUT, PAGE, DEST = sys.argv[1], sys.argv[2], sys.argv[3]   # output folder, e.g. pages/pasadena.html, destination file
def load(path):
    t = open(path).read(); return json.loads(t[t.index(',') + 1:t.rindex(')')])
meta = load(os.path.join(OUT, 'search', 'meta.js'))
p = next(i for i, pg in enumerate(meta['pages']) if pg[1] == PAGE)
name = meta['pages'][p][0]
calls, ains = [], set()
for f in sorted(glob.glob(os.path.join(OUT, 'search', 'apn', '*.js'))):
    rows = [r[:3] + [0] for r in load(f) if r[3] == p]
    if rows: ains.update(r[0] for r in rows); calls.append((f'apn/{os.path.basename(f)[:-3]}', rows))
for f in sorted(glob.glob(os.path.join(OUT, 'search', 'addr', '*.js'))):
    rows = [r for r in load(f) if r[5] in ains]
    if rows: calls.append((f'addr/{os.path.basename(f)[:-3]}', rows))
calls.insert(0, ('meta', {'pages': [[name, PAGE, '']], 'cities': meta['cities']}))
inject = '<script>// embedded search index for this page only\n' + '\n'.join(f'__S({json.dumps(k)},{json.dumps(v, separators=(",", ":"))});' for k, v in calls) + '</script>\n'
html = open(os.path.join(OUT, PAGE)).read()
def rep(a, b):
    global html
    assert html.count(a) == 1, ('MISSING', a[:80]); html = html.replace(a, b)
rep('"back": "../LA_County_Overview.html"', '"back": null')
rep('placeholder="Search address or APN…"', f'placeholder="Search {name} address or APN…"')
rep(' Click the dark area outside this map to open the neighboring area\'s parcel map.', '')
rep('</body></html>', inject + '</body></html>')
open(DEST, 'w').write(html)
print(f'{name}: {len(ains):,} APNs, {sum(len(v) for k, v in calls if k.startswith("addr")):,} addresses embedded | {len(html)/1e6:.1f} MB -> {DEST}')
