"""ShelfLife food shelf-life API, built on USDA FSIS FoodKeeper data (CC0)."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Literal

import json

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .data import DEFAULT_PATH, FoodKeeper

class PrettyJSONResponse(JSONResponse):
    """Indented JSON so responses are readable in a browser (apps parse it the same way)."""

    def render(self, content) -> bytes:
        return json.dumps(jsonable_encoder(content), indent=2, ensure_ascii=False).encode("utf-8")


app = FastAPI(
    default_response_class=PrettyJSONResponse,
    title="ShelfLife API",
    description="Search how long foods last in the pantry, fridge and freezer. "
    "Data: USDA FSIS FoodKeeper (public domain).",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@lru_cache
def db() -> FoodKeeper:
    return FoodKeeper.load()


@app.exception_handler(Exception)
async def data_load_error(request: Request, exc: Exception):
    return PrettyJSONResponse(
        status_code=503,
        content={
            "error": f"{type(exc).__name__}: {exc}",
            "fix": "The USDA data could not be loaded. Open "
            "https://www.fsis.usda.gov/shared/data/EN/foodkeeper.json in your browser, save it as "
            f"'foodkeeper.json' into: {DEFAULT_PATH.parent}  then restart the API.",
        },
    )


Location = Literal["pantry", "refrigerator", "freezer"]
Condition = Literal["unopened", "from_purchase_date", "after_opening", "after_thawing"]


@app.get("/")
def root():
    return {"name": "ShelfLife API", "docs": "/docs", "items": len(db().foods), "source": "USDA FSIS FoodKeeper"}


@app.get("/search")
def search(q: str = Query(..., min_length=1, description="Food name, e.g. 'milk'"), limit: int = Query(10, ge=1, le=50)):
    hits = db().search(q, limit)
    return {"query": q, "count": len(hits), "results": [{**f.to_dict(), "score": s} for f, s in hits]}


@app.get("/items/{item_id}")
def get_item(item_id: int):
    food = db().foods.get(item_id)
    if not food:
        raise HTTPException(404, "Item not found")
    return food.to_dict()


@app.get("/categories")
def categories():
    return sorted(db().categories.values(), key=lambda c: (c["name"] or "", c["subcategory"] or ""))


@app.get("/categories/{category_id}/items")
def category_items(category_id: int):
    if category_id not in db().categories:
        raise HTTPException(404, "Category not found")
    items = [f.to_dict() for f in db().foods.values() if f.category_id == category_id]
    return {"category": db().categories[category_id], "count": len(items), "items": items}


@app.get("/expiry")
def expiry(
    q: str | None = Query(None, description="Food name (best match is used)"),
    item_id: int | None = Query(None),
    location: Location = "refrigerator",
    condition: Condition | None = Query(None, description="Defaults to the first available entry for the location"),
    start_date: date | None = Query(None, description="Purchase/open date; defaults to today"),
):
    """Estimate the 'use by' date range for a food stored a given way."""
    if item_id is not None:
        food = db().foods.get(item_id)
    elif q:
        hits = db().search(q, 1)
        food = hits[0][0] if hits else None
    else:
        raise HTTPException(400, "Provide q or item_id")
    if not food:
        raise HTTPException(404, "No matching food")

    options = [s for s in food.storage if s["location"] == location and (condition is None or s["condition"] == condition)]
    timed = [s for s in options if s["min_days"] is not None]
    if not timed:
        guidance = options[0]["unit"] if options else None
        return {
            "item": {"id": food.id, "name": food.display_name},
            "location": location,
            "condition": options[0]["condition"] if options else None,
            "estimate": None,
            "status": "no_estimate",
            "guidance": guidance or f"USDA has no {location} guidance for this food",
            "tips": options[0]["tips"] if options else None,
            "available": [{k: s[k] for k in ("location", "condition", "min", "max", "unit", "tips")} for s in food.storage],
        }
    s = timed[0]
    start = start_date or date.today()
    earliest = start + timedelta(days=int(s["min_days"]))
    latest = start + timedelta(days=int(s["max_days"]))
    today = date.today()
    status = "fresh" if today < earliest else ("use_soon" if today <= latest else "expired")
    return {
        "item": {"id": food.id, "name": food.display_name},
        "location": location,
        "condition": s["condition"],
        "start_date": start,
        "use_by_earliest": earliest,
        "use_by_latest": latest,
        "days_left": (latest - today).days,
        "status": status,
        "tips": s["tips"],
    }
