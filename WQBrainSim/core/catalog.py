import json
import math

from .client import APIError, paged

MAX_TEXT = 2000
OPERATOR_KEYS = ("name", "category", "scope", "definition", "description", "level")
DATASET_KEYS = ("id", "name", "description", "category", "subcategory", "region", "delay",
                "universe", "coverage", "valueScore", "userCount", "alphaCount", "fieldCount")
FIELD_KEYS = ("id", "description", "dataset", "category", "subcategory", "region", "delay",
              "universe", "type", "coverage", "userCount", "alphaCount")


def _clean(value):
    """Keep plain scalars, short text, lists of those, and {id, name} references."""
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, list):
        return [v for v in (_clean(x) for x in value if not isinstance(x, (dict, list))) if v is not None]
    if isinstance(value, dict):
        ref = {k: _clean(value.get(k)) for k in ("id", "name") if isinstance(value.get(k), (str, int))}
        return ref or None
    return None


def _plain(value, depth=0):
    if depth > 12 or value is None or type(value) in (bool, str, int, float):
        return None if depth > 12 else _clean(value)
    if isinstance(value, list):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key)[:200]: _plain(item, depth + 1) for key, item in value.items()}
    return None


def _picker(keys):
    def pick(item):
        if not isinstance(item, dict):
            raise APIError("Catalog response contains a malformed entry.")
        return {k: _clean(item[k]) for k in keys if k in item}
    return pick


def _scope(region, delay, universe, instrument_type):
    if not all(isinstance(v, str) and v.strip() for v in (region, universe, instrument_type)):
        raise ValueError("region, universe and instrument_type must be nonempty strings.")
    if type(delay) is not int or delay < 0:
        raise ValueError("delay must be a nonnegative integer.")
    return {"instrumentType": instrument_type, "region": region, "delay": delay, "universe": universe}


def list_operators(client, *, deadline: float = 120) -> dict:
    end = client.clock() + deadline
    client.authenticate(end)
    data, _ = client.fetch("/operators", end)
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise APIError("Unexpected response shape from /operators.")
    return {"count": len(rows), "results": [_picker(OPERATOR_KEYS)(item) for item in rows]}


def list_settings(client, *, deadline: float = 120) -> dict:
    end = client.clock() + deadline
    client.authenticate(end)
    response = client.request("OPTIONS", "/simulations", end)
    try:
        settings = response.json()["actions"]["POST"]["settings"]["children"]
    except (ValueError, KeyError, TypeError):
        raise APIError(f"Unexpected response (HTTP {response.status_code}) from OPTIONS /simulations.") from None
    return {"results": _plain(settings)}


def list_datasets(client, *, region: str, delay: int, universe: str, instrument_type: str = "EQUITY",
                  max_items: int = 500, deadline: float = 300) -> dict:
    end = client.clock() + deadline
    client.authenticate(end)
    params = _scope(region, delay, universe, instrument_type)
    return {"query": params, **paged(client, "/data-sets", params, _picker(DATASET_KEYS), end, max_items)}


def list_data_fields(client, *, region: str, delay: int, universe: str, instrument_type: str = "EQUITY",
                     dataset: str | None = None, search: str | None = None, max_items: int = 1000,
                     deadline: float = 600) -> dict:
    end = client.clock() + deadline
    client.authenticate(end)
    params = _scope(region, delay, universe, instrument_type)
    if dataset is not None:
        params["dataset.id"] = str(dataset)
    if search is not None:
        params["search"] = str(search)
    return {"query": params, **paged(client, "/data-fields", params, _picker(FIELD_KEYS), end, max_items)}


def save_catalog(path: str, catalog: dict) -> None:
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(catalog, handle, indent=2, allow_nan=False)
        handle.write("\n")
