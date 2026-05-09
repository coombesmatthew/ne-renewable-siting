"""Tool definitions and executors for Claude tool-use chat.

Each tool here is a thin wrapper around the same helpers used by the
HTTP routers, so the model gets the exact same data the UI does.

The four tools cover the read-only spatial questions an end-user is
likely to ask in chat:

* ``get_parcel`` — lookup-by-id or point-in-polygon
* ``search_substations`` — substring search by name
* ``search_repd`` — filter the renewable energy planning database
* ``sample_renewables_at`` — point sample of solar/wind rasters
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

    return {"error": f"unknown tool {name}"}
