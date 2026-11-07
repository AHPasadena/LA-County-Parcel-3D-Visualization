# LA County Parcel 3D Visualization

A 3D map of how much property tax each parcel in Los Angeles County sends to its city — and how that depends on
when the building was built and when the property was last reassessed under Prop 13.

**Live map:** https://ahpasadena.github.io/LA-County-Parcel-3D-Visualization/

- **Height** = estimated annual property tax to the parcel's city (or the County general fund where unincorporated).
- **Color** = year built, or year last reassessed (Prop 13 base year).
- Click a city on the county overview to open its parcel-level map. Los Angeles is split by Community Plan Area and
  unincorporated LA County by official community (Altadena, East Los Angeles, …).
- Search by address or APN; filter by year, assessed value and property type (single-family, condo, multifamily,
  mixed-use, commercial, industrial, institutional, vacant); hover or tap a parcel for its estimated taxes.

## How the numbers are estimated

All dollar figures are **estimates** for FY 2026-27 from each parcel's net taxable value on the 2026 assessment roll:

- **Est. property tax paid** = net taxable value × the tax rate area's total secured rate (the 1% general levy plus
  voter-approved bonds), from the Auditor-Controller's CR12 report.
- **City revenue** = the share of the 1% levy apportioned to the parcel's city in its tax rate area (all of the city's
  accounts), from the Auditor-Controller's AF49 report; the County general fund's share where unincorporated.
- **Not included:** direct assessments and special taxes billed as separate line items (lighting, sewer, Measure W,
  Measure B, Mello-Roos, …), supplemental and unsecured bills, sales/hotel/utility taxes.
- Parcels sharing an identical footprint (condo units) are combined into one footprint and summed.
- Units and year built come from the Assessor's building records. About 16,500 improved residential parcels (~0.8%)
  have none, often parcels split off after the building was enrolled, and show "units not on the Assessor's record".

## Where the data comes from (fetched automatically)

| Data | Source | Fetched by |
|---|---|---|
| Assessor roll (values, year built, base year, use, address) | LA County ArcGIS: [Assessor Parcel Data Roll 2026](https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/Assessor%20Parcel%20Data%20Roll%202026/FeatureServer/0) | `fetch_county_data.py` |
| Parcel shapes | LA County ArcGIS: [ASSR_PARCELS_25_View](https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/ASSR_PARCELS_25_View/FeatureServer/0) | `fetch_county_data.py` |
| Unincorporated communities | LA County ArcGIS: [Countywide Statistical Areas (348)](https://services.arcgis.com/RmCCgQtiZLDCtblq/ArcGIS/rest/services/Countywide_Statistical_Areas_with_348_polygons/FeatureServer/0) | `fetch_county_data.py` |
| LA Community Plan Areas | LA City Planning: [Community Plan Areas](https://services5.arcgis.com/7nsPwEMP38bSkCjy/ArcGIS/rest/services/Community_Plan_Areas/FeatureServer/0) | `fetch_county_data.py` |
| City share of the 1% levy (AF49) | [LA County Auditor-Controller, Property Tax Report Central](https://auditor.lacounty.gov/property-tax-report-central/) | stored in `data/` (updated yearly) |
| Tax rates incl. voter-approved bonds (CR12) | same | stored in `data/` (updated yearly) |

Nothing large is stored in this repository. A GitHub Actions workflow
([`.github/workflows/build.yml`](.github/workflows/build.yml)) runs monthly — or on demand from the **Actions** tab →
**Build and publish map** → **Run workflow** — fetches the County data, rebuilds the maps, and publishes them to
GitHub Pages.

## Yearly update (each fall, after the County publishes the new roll and tax rates)

1. In `fetch_county_data.py`, update `ROLL_YEAR` and the roll/parcel service URLs to the new year's County services.
2. From [Property Tax Report Central](https://auditor.lacounty.gov/property-tax-report-central/), download the new
   **AF49 Modified ATI Ratios** and **CR12 Secured Tax Rates and Ratios** reports, gzip the `.txt` files into `data/`,
   and update the default file names at the top of `build_county.py`.
3. Update the year ranges (`BUILT`, `BASE`) in `build_county.py` and `DIMS` in `county_revenue_template.html`.
4. Run the workflow.

## Building on your own computer (optional)

```bash
pip install -r requirements.txt
python fetch_county_data.py --out work --city PASADENA     # one city, ~1 minute; omit --city for the whole county
python build_county.py --roll work/roll.csv --parcels work/parcels.gpkg \
  --cpa work/la_cpa.geojson --uninc work/unincorporated.geojson --out site
open site/index.html
```

A full county build needs about 10 GB of memory. To share a single area as one self-contained file (search included):

```bash
python make_standalone_page.py site pages/pasadena.html Pasadena_Parcel_Revenue.html
```

Maps use [MapLibre GL JS](https://maplibre.org/) with an [OpenFreeMap](https://openfreemap.org/) basemap
(© OpenMapTiles, data © OpenStreetMap contributors).
