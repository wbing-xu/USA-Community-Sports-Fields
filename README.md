# USA Community Sports Fields

Scripts for harvesting community sports field data across the United States from OpenStreetMap's Overpass API. The tooling collects individual pitches (soccer, American football, baseball/softball, multipurpose), assigns them to parent parks/schools/recreation complexes, and exports both raw and aggregated CSV files.

## Requirements
- Python 3.10+
- Packages: `requests`, `pandas`, `geopandas`, `shapely`, `tqdm`, `retrying`

## Usage
The main entry point is `fetch_fields.py`.

```bash
python fetch_fields.py --output-dir data
```

Key options:
- `--states`: Optional list of ISO3166-2 state codes (e.g., `US-CA US-NY`). Defaults to all states defined in the script.
- `--max-states`: Limit how many states to run in one invocation for testing.
- `--output-dir`: Destination for `fields_raw.csv` and `facility_summary.csv`.

The script automatically:
1. Queries pitch geometry per state via Overpass.
2. Pulls park/recreation/school/sports complex polygons for spatial joins.
3. Computes field areas and centroids.
4. Assigns each field to the highest-priority containing facility (school → recreation ground → park → complex).
5. Writes two CSV outputs:
   - `fields_raw.csv`: pitch-level records with geometry-derived centroids, areas, inferred address tags, and facility assignment columns.
   - `facility_summary.csv`: facility-level rollups with per-sport counts and totals.

### Performance, anti-ban, and resume behavior

- Use `--workers` (default `3`) to parallelize state downloads while jittered backoff and rotating Overpass endpoints/user agents reduce anti-scraping triggers.
- Each completed state is appended immediately to `fields_raw.csv`, `facility_summary.csv`, and `processed_states.txt` so crashes keep already gathered data.
- Reruns will skip states already listed in the checkpoint or present in the CSVs, enabling safe, incremental progress across sessions.

## Facility matching utilities
`facility_matching.py` holds the spatial join logic and aggregation helpers. It can be imported independently if you want to run custom post-processing on previously collected field data.
