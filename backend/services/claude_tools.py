"""Tool definitions and executors for Claude tool-use chat.

Each tool here is a thin wrapper around the same helpers used by the
HTTP routers, so the model gets the exact same data the UI does.

The six tools cover the read-only spatial questions an end-user is
likely to ask in chat:

* ``get_parcel`` — lookup-by-id or point-in-polygon
* ``find_parcels`` — broad parcel-attribute search
* ``search_substations`` — substring search by name
* ``search_repd`` — filter the renewable energy planning database
* ``sample_renewables_at`` — point sample of solar/wind rasters
* ``search_ownership`` — HM Land Registry CCOD ownership lookup
"""

from __future__ import annotations

from typing import Any

from backend.services.data_store import get_data_store
from backend.services.raster_sampler import sample_raster_at

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "get_parcel",
        "description": (
            "Look up parcel attributes. Either provide parcel_id (e.g. 'NE-001234') "
            "OR lng+lat to find the parcel containing that point. Returns area_ha, "
            "mean_pvout_kwhkwp, mean_wind_speed_100m_ms, dist_substation_gen_headroom_m, "
            "dist_substation_any_headroom_m, nearest_substation_name, lad_code, lad_name, "
            "plus boolean intersects_* flags (aonb, national_park, green_belt, sssi, flood) "
            "and the parcel centroid."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parcel_id": {
                    "type": "string",
                    "description": "Parcel identifier like NE-001234",
                },
                "lng": {"type": "number"},
                "lat": {"type": "number"},
            },
        },
    },
    {
        "name": "find_parcels",
        "description": (
            "Search the 33,363 NE England parcels by attribute filters. Returns up to "
            "`limit` parcels (default 10), sorted by area_ha descending. Use this when "
            "the user asks broad parcel-attribute questions like 'find parcels >10 ha "
            "with wind > 8 m/s within 5 km of a 33 kV substation and no AONB overlap'. "
            "Each result includes parcel_id, area_ha, centroid_lon, centroid_lat, "
            "lad_name, mean_pvout_kwhkwp, mean_wind_speed_100m_ms, "
            "dist_substation_gen_headroom_m, dist_substation_any_headroom_m, "
            "nearest_substation_name, and the 7 boolean intersects_* flags."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "min_pvout_kwhkwp": {
                    "type": "number",
                    "description": "kWh/kWp/yr",
                },
                "min_wind_speed_100m_ms": {"type": "number"},
                "max_dist_substation_gen_headroom_m": {
                    "type": "number",
                    "description": "metres",
                },
                "max_dist_substation_any_headroom_m": {"type": "number"},
                "min_voltage_kv": {
                    "type": "string",
                    "enum": ["11", "20", "33", "66", "132"],
                    "description": (
                        "Required minimum substation voltage. Filters against "
                        "dist_genhr_min_<voltage>kv_m. Combined with "
                        "max_dist_substation_gen_headroom_m to mean 'within X "
                        "metres of a substation at >=this voltage with gen headroom'."
                    ),
                },
                "min_area_ha": {"type": "number"},
                "exclude_aonb": {"type": "boolean"},
                "exclude_national_park": {"type": "boolean"},
                "exclude_green_belt": {"type": "boolean"},
                "exclude_sssi": {"type": "boolean"},
                "exclude_flood": {"type": "boolean"},
                "exclude_listed_building": {"type": "boolean"},
                "exclude_scheduled_monument": {"type": "boolean"},
                "lad_code": {
                    "type": "string",
                    "description": "ONS LAD24CD",
                },
                "lad_name": {
                    "type": "string",
                    "description": "case-insensitive substring match",
                },
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "search_substations",
        "description": (
            "Find Northern Powergrid substations by name substring (case-insensitive). "
            "Returns up to `limit` matching substations with name, type (GSP/BSP/Primary), "
            "pvoltage (kV), firm_cap (MVA), genhr (gen headroom MW), demhr (dem headroom MW), "
            "constraint colours, and centroid lon/lat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Name substring, case-insensitive",
                },
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["q"],
        },
    },
    {
        "name": "search_repd",
        "description": (
            "Search the DESNZ Renewable Energy Planning Database (NE England subset) "
            "for projects matching filters. Returns count, total_matched, and a list of "
            "projects with site_name, operator, technology_type, development_status, "
            "capacity_mw, lon, lat, county, region, planning_application_reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tech": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Technology types like ['Solar Photovoltaics', 'Wind Onshore', "
                        "'Battery', 'Small Hydro']"
                    ),
                },
                "status": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Status values like ['Operational', 'Under Construction', "
                        "'Planning Permission Granted']"
                    ),
                },
                "min_capacity_mw": {"type": "number"},
                "max_capacity_mw": {"type": "number"},
                "bbox": {
                    "type": "string",
                    "description": "Comma-separated minlon,minlat,maxlon,maxlat",
                },
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "sample_renewables_at",
        "description": (
            "Sample the solar PVOUT (kWh/kWp/yr) and 100 m wind speed (m/s) rasters "
            "at an arbitrary lon/lat. Useful for points that aren't inside a parcel, "
            "e.g. checking the resource at a specific landmark like 'top of Cheviot Hills'. "
            "Returns {lng, lat, pvout_kwhkwp, wind_speed_100m_ms}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lng": {"type": "number"},
                "lat": {"type": "number"},
            },
            "required": ["lng", "lat"],
        },
    },
    {
        "name": "search_ownership",
        "description": (
            "Search HM Land Registry's Commercial and Corporate Ownership Data (CCOD) "
            "for properties owned by UK-registered companies in NE England. EXCLUDES "
            "individual private owners — those aren't in the dataset (~70% of agricultural "
            "land is privately held). Use for queries like 'who owns properties at NE10 1XX', "
            "'list properties owned by Lightsource Renewable Energy', or 'how many properties "
            "does Thirteen Housing Group own'. Each result includes proprietor_name_1, "
            "company_registration_no_1, proprietorship_category_1, property_address, postcode, "
            "district, tenure, title_number, and lon/lat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proprietor_name": {
                    "type": "string",
                    "description": "Case-insensitive substring match against proprietor name.",
                },
                "postcode": {
                    "type": "string",
                    "description": "Exact postcode match (case-insensitive, spaces ignored).",
                },
                "title_number": {
                    "type": "string",
                    "description": "HM Land Registry title number.",
                },
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
]


def execute_tool(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a Claude tool call and return a JSON-friendly dict.

    Errors are returned in the result payload (rather than raised) so
    that the model can recover and retry / explain to the user.
    """

    ds = get_data_store()

    if name == "get_parcel":
        from backend.routers.parcel import _find_by_id, _find_parcel_at, _row_to_parcel

        parcel_id = tool_input.get("parcel_id")
        if parcel_id:
            row = _find_by_id(ds.parcels, str(parcel_id))
            if row is None:
                return {"error": f"parcel {parcel_id} not found"}
            return _row_to_parcel(row).model_dump()
        if "lng" in tool_input and "lat" in tool_input:
            try:
                lng = float(tool_input["lng"])
                lat = float(tool_input["lat"])
            except (TypeError, ValueError):
                return {"error": "lng and lat must be numbers"}
            row = _find_parcel_at(ds.parcels, lng, lat)
            if row is None:
                return {"error": "no parcel at that lng/lat"}
            return _row_to_parcel(row).model_dump()
        return {"error": "supply parcel_id or lng+lat"}

    if name == "find_parcels":
        df = ds.parcels  # GeoDataFrame
        f = df

        # Numeric "min" filters
        for col, key in [
            ("mean_pvout_kwhkwp", "min_pvout_kwhkwp"),
            ("mean_wind_speed_100m_ms", "min_wind_speed_100m_ms"),
            ("area_ha", "min_area_ha"),
        ]:
            if tool_input.get(key) is not None:
                try:
                    f = f[f[col] >= float(tool_input[key])]
                except (TypeError, ValueError):
                    return {"error": f"{key} must be a number"}

        # Distance filters: voltage tier (use the right column) + any-hr distance
        if tool_input.get("max_dist_substation_gen_headroom_m") is not None:
            v = tool_input.get("min_voltage_kv") or "11"
            col = f"dist_genhr_min_{v}kv_m"
            if col not in f.columns:
                return {"error": f"unknown voltage column {col}"}
            try:
                f = f[f[col] <= float(tool_input["max_dist_substation_gen_headroom_m"])]
            except (TypeError, ValueError):
                return {"error": "max_dist_substation_gen_headroom_m must be a number"}
        if tool_input.get("max_dist_substation_any_headroom_m") is not None:
            try:
                f = f[
                    f["dist_substation_any_headroom_m"]
                    <= float(tool_input["max_dist_substation_any_headroom_m"])
                ]
            except (TypeError, ValueError):
                return {"error": "max_dist_substation_any_headroom_m must be a number"}

        # Boolean exclusions
        for excl, col in [
            ("exclude_aonb", "intersects_aonb"),
            ("exclude_national_park", "intersects_national_park"),
            ("exclude_green_belt", "intersects_green_belt"),
            ("exclude_sssi", "intersects_sssi"),
            ("exclude_flood", "intersects_flood"),
            ("exclude_listed_building", "intersects_listed_building"),
            ("exclude_scheduled_monument", "intersects_scheduled_monument"),
        ]:
            if tool_input.get(excl):
                f = f[~f[col].astype(bool)]

        # LAD filters
        if tool_input.get("lad_code"):
            f = f[f["lad_code"] == str(tool_input["lad_code"])]
        if tool_input.get("lad_name"):
            needle = str(tool_input["lad_name"]).lower()
            f = f[f["lad_name"].str.lower().str.contains(needle, na=False)]

        total_matched = int(len(f))
        try:
            limit = int(tool_input.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        f = f.sort_values("area_ha", ascending=False).head(limit)

        cols = [
            "parcel_id",
            "area_ha",
            "centroid_lon",
            "centroid_lat",
            "lad_name",
            "mean_pvout_kwhkwp",
            "mean_wind_speed_100m_ms",
            "dist_substation_gen_headroom_m",
            "dist_substation_any_headroom_m",
            "nearest_substation_name",
            "intersects_aonb",
            "intersects_national_park",
            "intersects_green_belt",
            "intersects_sssi",
            "intersects_flood",
            "intersects_listed_building",
            "intersects_scheduled_monument",
        ]
        cols = [c for c in cols if c in f.columns]
        # Drop the geometry; keep only attribute columns we want.
        records = f[cols].to_dict(orient="records")
        # Coerce numpy bools / NaN-safe coerce so JSON encoding is happy.
        cleaned_results: list[dict[str, Any]] = []
        for rec in records:
            clean: dict[str, Any] = {}
            for k, v in rec.items():
                if k.startswith("intersects_"):
                    clean[k] = bool(v)
                else:
                    clean[k] = v
            cleaned_results.append(clean)
        return {
            "count": len(cleaned_results),
            "total_matched": total_matched,
            "results": cleaned_results,
        }

    if name == "search_substations":
        from backend.routers.substation import _search_substations

        q = str(tool_input.get("q", ""))
        limit = int(tool_input.get("limit", 10))
        results = _search_substations(ds.substation_catchments, q, limit)
        return {
            "count": len(results),
            "results": [r.model_dump() for r in results],
        }

    if name == "search_repd":
        from backend.routers.repd import _filter_repd

        try:
            return _filter_repd(ds.repd, **tool_input)
        except ValueError as exc:
            return {"error": str(exc)}

    if name == "sample_renewables_at":
        try:
            lng = float(tool_input["lng"])
            lat = float(tool_input["lat"])
        except (KeyError, TypeError, ValueError):
            return {"error": "lng and lat are required and must be numbers"}
        return {
            "lng": lng,
            "lat": lat,
            "pvout_kwhkwp": sample_raster_at(lng, lat, ds.solar_tif_path),
            "wind_speed_100m_ms": sample_raster_at(lng, lat, ds.wind_tif_path),
        }

    if name == "search_ownership":
        from backend.routers.ownership import _by_postcode, _by_proprietor, _by_title

        df = ds.ccod
        try:
            limit = int(tool_input.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        if tool_input.get("title_number"):
            rows = _by_title(df, str(tool_input["title_number"]))[:limit]
            return {"count": len(rows), "results": [r.model_dump() for r in rows]}
        if tool_input.get("postcode"):
            rows = _by_postcode(df, str(tool_input["postcode"]), limit)
            return {"count": len(rows), "results": [r.model_dump() for r in rows]}
        if tool_input.get("proprietor_name"):
            rows = _by_proprietor(df, str(tool_input["proprietor_name"]), limit)
            return {"count": len(rows), "results": [r.model_dump() for r in rows]}
        return {"error": "supply at least one of: proprietor_name, postcode, title_number"}

    return {"error": f"unknown tool {name}"}


def _summarize_tool_result(name: str, result: Any) -> str:
    """Return a one-line UX badge summary for a tool result.

    Used by the chat handler to emit ``tool_result`` SSE events that
    surface as inline chips in the frontend.
    """

    if isinstance(result, dict) and "error" in result:
        return f"error: {result['error']}"
    if name == "find_parcels":
        return (
            f"Found {result.get('count', 0)} of {result.get('total_matched', '?')} matching parcels"
        )
    if name == "search_substations":
        return f"Found {result.get('count', 0)} substations"
    if name == "search_repd":
        return f"Found {result.get('count', 0)} of {result.get('total_matched', '?')} REPD projects"
    if name == "search_ownership":
        return f"Found {result.get('count', 0)} ownership records"
    if name == "get_parcel":
        if isinstance(result, dict) and "parcel_id" in result:
            return f"Parcel {result['parcel_id']}, {result.get('area_ha', '?')} ha"
        return "Parcel lookup"
    if name == "sample_renewables_at":
        if not isinstance(result, dict):
            return "Done"
        pv = result.get("pvout_kwhkwp")
        ws = result.get("wind_speed_100m_ms")
        if pv is not None:
            return f"PVOUT {pv} kWh/kWp/yr, wind {ws} m/s"
        return "Off-raster"
    return "Done"
