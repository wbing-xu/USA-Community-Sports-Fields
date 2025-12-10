"""Utilities for matching sports fields to nearby community facilities.

This module keeps the spatial matching logic separate from the data
collection flow.  It expects GeoDataFrames with WGS84 coordinates and
returns both enriched field data and facility rollups.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

FACILITY_PRIORITY = {
    "school": 0,
    "recreation": 1,
    "park": 2,
    "complex": 3,
    "other": 4,
}


@dataclass
class Facility:
    """Representation of a facility polygon returned from OSM."""

    name: str
    facility_type: str
    osm_id: str
    geometry: Polygon


def assign_facilities(
    fields: gpd.GeoDataFrame, facilities: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Attach facility metadata to each sports field.

    The function uses a spatial join to find the facility polygons that contain
    each field and resolves conflicts based on :data:`FACILITY_PRIORITY`.

    Parameters
    ----------
    fields:
        GeoDataFrame with a ``geometry`` column.
    facilities:
        GeoDataFrame with columns ``facility_type``, ``name``, ``osm_id``.

    Returns
    -------
    GeoDataFrame
        Copy of ``fields`` with ``parent_facility_*`` columns populated.
    """
    if facilities.empty:
        # Nothing to match, return the input unchanged.
        fields = fields.copy()
        fields["parent_facility_name"] = None
        fields["parent_facility_type"] = None
        fields["parent_facility_osm_id"] = None
        fields["facility_lat"] = None
        fields["facility_lon"] = None
        return fields

    facilities = facilities.copy()
    facilities["priority"] = facilities["facility_type"].map(FACILITY_PRIORITY).fillna(
        FACILITY_PRIORITY["other"]
    )

    joined = gpd.sjoin(fields, facilities, how="left", predicate="within")
    if joined.empty:
        fields = fields.copy()
        fields["parent_facility_name"] = None
        fields["parent_facility_type"] = None
        fields["parent_facility_osm_id"] = None
        fields["facility_lat"] = None
        fields["facility_lon"] = None
        return fields

    # Pick the best facility per field based on the priority mapping.
    joined = joined.sort_values(["index_left", "priority"])
    best = joined.groupby("index_left").first()

    centroids = facilities.geometry.centroid
    facility_lat = best["index_right"].map(lambda idx: centroids[idx].y if pd.notna(idx) else None)
    facility_lon = best["index_right"].map(lambda idx: centroids[idx].x if pd.notna(idx) else None)

    enriched = fields.copy()
    enriched["parent_facility_name"] = best["name"]
    enriched["parent_facility_type"] = best["facility_type"]
    enriched["parent_facility_osm_id"] = best["osm_id"]
    enriched["facility_lat"] = facility_lat.values
    enriched["facility_lon"] = facility_lon.values

    # Keep field coordinates if a facility centroid is missing.
    enriched["facility_lat"] = enriched["facility_lat"].fillna(enriched.get("lat"))
    enriched["facility_lon"] = enriched["facility_lon"].fillna(enriched.get("lon"))
    return enriched


def summarize_facilities(enriched_fields: gpd.GeoDataFrame) -> pd.DataFrame:
    """Aggregate fields into facility-level counts."""

    lat_col = "facility_lat" if "facility_lat" in enriched_fields else "lat"
    lon_col = "facility_lon" if "facility_lon" in enriched_fields else "lon"

    summary = (
        enriched_fields.groupby(
            [
                "parent_facility_name",
                "parent_facility_type",
                "city",
                "state",
                lon_col,
                lat_col,
            ],
            dropna=False,
        )
        .agg(
            soccer_count=("is_soccer", "sum"),
            football_count=("is_football", "sum"),
            baseball_count=("is_baseball", "sum"),
            multipurpose_count=("is_multi", "sum"),
            total_fields=("field_id", "count"),
        )
        .reset_index()
    )

    summary = summary.rename(
        columns={
            "parent_facility_name": "facility_name",
            "parent_facility_type": "facility_type",
            lon_col: "lon",
            lat_col: "lat",
        }
    )

    return summary


def prepare_facility_frame(facilities: Iterable[Facility]) -> gpd.GeoDataFrame:
    """Convert facility dataclass instances into a GeoDataFrame."""

    records: List[Dict[str, str]] = []
    geometries = []
    for facility in facilities:
        records.append(
            {
                "name": facility.name,
                "facility_type": facility.facility_type,
                "osm_id": facility.osm_id,
            }
        )
        geometries.append(facility.geometry)

    gdf = gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")
    return gdf
