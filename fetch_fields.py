"""Fetch sports field data from OpenStreetMap using the Overpass API.

The script pulls pitch-level features for multiple sports across all U.S. states,
associates each field with the park/school/recreation complex that contains it,
and exports both the raw list and a facility-level summary.
"""
from __future__ import annotations

import argparse
import itertools
import os
import random
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import geopandas as gpd
import pandas as pd
import requests
from retrying import retry
from shapely.geometry import Point, Polygon
from tqdm import tqdm

from facility_matching import Facility, assign_facilities, prepare_facility_frame, summarize_facilities

OVERPASS_ENDPOINTS: Tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
STATE_CODES = {
    "Alabama": "US-AL",
    "Alaska": "US-AK",
    "Arizona": "US-AZ",
    "Arkansas": "US-AR",
    "California": "US-CA",
    "Colorado": "US-CO",
    "Connecticut": "US-CT",
    "Delaware": "US-DE",
    "District of Columbia": "US-DC",
    "Florida": "US-FL",
    "Georgia": "US-GA",
    "Hawaii": "US-HI",
    "Idaho": "US-ID",
    "Illinois": "US-IL",
    "Indiana": "US-IN",
    "Iowa": "US-IA",
    "Kansas": "US-KS",
    "Kentucky": "US-KY",
    "Louisiana": "US-LA",
    "Maine": "US-ME",
    "Maryland": "US-MD",
    "Massachusetts": "US-MA",
    "Michigan": "US-MI",
    "Minnesota": "US-MN",
    "Mississippi": "US-MS",
    "Missouri": "US-MO",
    "Montana": "US-MT",
    "Nebraska": "US-NE",
    "Nevada": "US-NV",
    "New Hampshire": "US-NH",
    "New Jersey": "US-NJ",
    "New Mexico": "US-NM",
    "New York": "US-NY",
    "North Carolina": "US-NC",
    "North Dakota": "US-ND",
    "Ohio": "US-OH",
    "Oklahoma": "US-OK",
    "Oregon": "US-OR",
    "Pennsylvania": "US-PA",
    "Rhode Island": "US-RI",
    "South Carolina": "US-SC",
    "South Dakota": "US-SD",
    "Tennessee": "US-TN",
    "Texas": "US-TX",
    "Utah": "US-UT",
    "Vermont": "US-VT",
    "Virginia": "US-VA",
    "Washington": "US-WA",
    "West Virginia": "US-WV",
    "Wisconsin": "US-WI",
    "Wyoming": "US-WY",
}

SPORT_ALIASES = {
    "soccer": ["soccer"],
    "football": ["american_football"],
    "baseball": ["baseball", "softball"],
    "multi": ["multi"],
}

FACILITY_TYPES = {
    ("leisure", "park"): "park",
    ("landuse", "recreation_ground"): "recreation",
    ("amenity", "school"): "school",
    ("leisure", "sports_centre"): "complex",
    ("leisure", "pitch"): "complex",
}


@dataclass
class OverpassResult:
    elements: List[dict]


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": f"usa-community-sports-fields/1.0 ({int(time.time())}-{random.randint(1000, 9999)})",
            "Accept": "application/json",
        }
    )
    return session


@retry(stop_max_attempt_number=6, wait_exponential_multiplier=2000, wait_exponential_max=20000)
def _post_query(query: str, endpoint: str, session: requests.Session) -> OverpassResult:
    response = session.post(endpoint, data=query.encode("utf-8"), timeout=120)
    response.raise_for_status()
    payload = response.json()
    return OverpassResult(elements=payload.get("elements", []))


def build_state_query(area_code: str, body: str) -> str:
    return f"""
[out:json][timeout:300];
area["ISO3166-2"="{area_code}"]->.searchArea;
{body}
out geom;
""".strip()


def pitch_query(area_code: str) -> str:
    body = """
(
  node(area.searchArea)["leisure"="pitch"];
  way(area.searchArea)["leisure"="pitch"];
  relation(area.searchArea)["leisure"="pitch"];
);
"""
    return build_state_query(area_code, body)


def facility_query(area_code: str) -> str:
    body = """
(
  way(area.searchArea)["leisure"="park"];
  relation(area.searchArea)["leisure"="park"];
  way(area.searchArea)["landuse"="recreation_ground"];
  relation(area.searchArea)["landuse"="recreation_ground"];
  way(area.searchArea)["amenity"="school"];
  relation(area.searchArea)["amenity"="school"];
  way(area.searchArea)["leisure"="sports_centre"];
  relation(area.searchArea)["leisure"="sports_centre"];
  way(area.searchArea)["leisure"="pitch"];
  relation(area.searchArea)["leisure"="pitch"];
);
"""
    return build_state_query(area_code, body)


def element_to_geometry(element: dict) -> Optional[Point | Polygon]:
    if element.get("type") == "node":
        return Point(element["lon"], element["lat"])

    coords = element.get("geometry")
    if not coords:
        return None

    points = [(c["lon"], c["lat"]) for c in coords]
    if len(points) < 3:
        return None

    if points[0] != points[-1]:
        points.append(points[0])
    return Polygon(points)


def classify_field(tags: Dict[str, str]) -> str:
    sport = tags.get("sport")
    if sport in SPORT_ALIASES["soccer"]:
        return "soccer"
    if sport in SPORT_ALIASES["football"]:
        return "football"
    if sport in SPORT_ALIASES["baseball"]:
        return "baseball"
    return "multipurpose"


def _extract_address(tags: Dict[str, str]) -> Dict[str, Optional[str]]:
    return {
        "city": tags.get("addr:city") or tags.get("is_in:city"),
        "state": tags.get("addr:state") or tags.get("is_in:state"),
        "postal_code": tags.get("addr:postcode"),
    }


def extract_fields(elements: Iterable[dict]) -> gpd.GeoDataFrame:
    records: List[dict] = []
    geometries = []

    for el in elements:
        geom = element_to_geometry(el)
        if geom is None:
            continue

        tags = el.get("tags", {})
        if tags.get("leisure") != "pitch":
            continue

        field_type = classify_field(tags)
        address = _extract_address(tags)
        centroid = geom.centroid
        area_m2 = 0
        if geom.geom_type == "Polygon":
            area_m2 = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(3857).area.iloc[0]

        record = {
            "field_id": f"{el.get('type')}:{el.get('id')}",
            "field_type": field_type,
            "lat": centroid.y,
            "lon": centroid.x,
            "field_area": area_m2,
            "city": address["city"],
            "state": address["state"],
            "postal_code": address["postal_code"],
            "is_soccer": field_type == "soccer",
            "is_football": field_type == "football",
            "is_baseball": field_type == "baseball",
            "is_multi": field_type == "multipurpose",
        }
        geometries.append(geom)
        records.append(record)

    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")


def extract_facilities(elements: Iterable[dict]) -> List[Facility]:
    facilities: List[Facility] = []
    for el in elements:
        geom = element_to_geometry(el)
        if geom is None:
            continue

        tags = el.get("tags", {})
        facility_type = None
        for (k, v), ftype in FACILITY_TYPES.items():
            if tags.get(k) == v:
                facility_type = ftype
                break

        if facility_type is None:
            continue

        name = tags.get("name", "Unknown facility")
        osm_id = f"{el.get('type')}:{el.get('id')}"
        facilities.append(
            Facility(name=name, facility_type=facility_type, osm_id=osm_id, geometry=geom)
        )
    return facilities


def _append_frame(frame: pd.DataFrame, path: Path) -> None:
    header = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, mode="a", index=False, header=header)


def _record_checkpoint(state_code: str, checkpoint_path: Path) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{state_code}\n")


def _load_processed_states(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    try:
        if path.suffix == ".csv":
            df = pd.read_csv(path, usecols=["state"])
            return set(df["state"].dropna().astype(str))
        return set(line.strip() for line in path.read_text().splitlines() if line.strip())
    except Exception:  # noqa: BLE001
        return set()


def _query_with_fallback(query: str, session: requests.Session) -> List[dict]:
    last_error: Optional[Exception] = None
    for attempt, endpoint in enumerate(itertools.cycle(OVERPASS_ENDPOINTS), start=1):
        if attempt > len(OVERPASS_ENDPOINTS) * 3:
            break
        try:
            result = _post_query(query, endpoint, session)
            return result.elements
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            # Jittered sleep to reduce ban risk
            time.sleep(random.uniform(1.0, 3.5))
            continue
    if last_error:
        raise last_error
    return []


def fetch_state(area_code: str, session: Optional[requests.Session] = None) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    session = session or _build_session()
    field_elements = _query_with_fallback(pitch_query(area_code), session)
    facility_elements = _query_with_fallback(facility_query(area_code), session)
    fields = extract_fields(field_elements)
    facilities = prepare_facility_frame(extract_facilities(facility_elements))

    if fields.empty:
        return fields, facilities

    if fields["state"].isna().all():
        fields["state"] = area_code.split("-")[-1]

    return fields, facilities


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch community sports fields via Overpass API")
    parser.add_argument(
        "--states",
        nargs="*",
        default=list(STATE_CODES.values()),
        help="List of ISO3166-2 state codes (e.g. US-CA US-NY). Defaults to all states.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory where fields_raw.csv and facility_summary.csv will be written.",
    )
    parser.add_argument(
        "--max-states",
        type=int,
        default=None,
        help="Limit the number of states processed in one run (useful for testing).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Number of concurrent state downloads to run.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    states = args.states
    if args.max_states is not None:
        states = states[: args.max_states]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fields_path = output_dir / "fields_raw.csv"
    summary_path = output_dir / "facility_summary.csv"
    checkpoint_path = output_dir / "processed_states.txt"

    processed_states: Set[str] = set()
    for path in (checkpoint_path, fields_path):
        processed_states |= _load_processed_states(path)

    remaining_states = [s for s in states if s not in processed_states]
    if not remaining_states:
        print("All requested states already processed. Nothing to do.")
        return

    lock = threading.Lock()

    def worker(state_code: str) -> Optional[Tuple[gpd.GeoDataFrame, pd.DataFrame, str]]:
        session = _build_session()
        try:
            fields, facilities = fetch_state(state_code, session=session)
        except Exception as exc:  # noqa: BLE001
            print(f"Error fetching {state_code}: {exc}", file=sys.stderr)
            return None
        if fields.empty:
            return None
        enriched = assign_facilities(fields, facilities)
        summary = summarize_facilities(enriched)
        return enriched, summary, state_code

    with tqdm(total=len(remaining_states), desc="States") as pbar:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(worker, state): state for state in remaining_states}
            for future in as_completed(futures):
                state_code = futures[future]
                result = future.result()
                if result is None:
                    pbar.update(1)
                    print(f"Failed or empty result for {state_code}", file=sys.stderr)
                    continue

                enriched, summary, state_code = result
                with lock:
                    _append_frame(enriched, fields_path)
                    _append_frame(summary, summary_path)
                    _record_checkpoint(state_code, checkpoint_path)
                pbar.update(1)

    print(f"Progress saved to {fields_path} and {summary_path} (checkpoint: {checkpoint_path}).")


if __name__ == "__main__":
    main()
