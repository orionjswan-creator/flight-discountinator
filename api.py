from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

import flight_discountinator as fd


app = FastAPI(
    title="Flight Discountinator API",
    version="1.0.0",
    description="Rank discounted flight destinations from a home airport (default CMH).",
)


@app.on_event("startup")
def startup_load_env() -> None:
    fd.load_dotenv()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/deals")
def deals(
    origin: str = Query(default="CMH"),
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    lookahead_days: int = Query(default=120, ge=1),
    top_destinations: int = Query(default=10, ge=1, le=100),
    probe_destinations: int = Query(default=20, ge=1, le=100),
    probe_workers: int = Query(default=4, ge=1, le=32),
    adults: int = Query(default=1, ge=1, le=9),
    nonstop: bool = Query(default=False),
    max_price: Optional[float] = Query(default=None, gt=0),
    base_url: str = Query(default="https://test.api.amadeus.com"),
    max_retries: int = Query(default=3, ge=1, le=10),
    retry_backoff: float = Query(default=1.0, ge=0.0, le=10.0),
) -> dict:
    if fd.requests is None:
        raise HTTPException(status_code=500, detail="Missing dependency: requests")

    try:
        origin_code = fd.normalize_origin(origin)
        start, end = fd.resolve_date_window(start_date, end_date, lookahead_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    client_id = fd.os.getenv("AMADEUS_CLIENT_ID")
    client_secret = fd.os.getenv("AMADEUS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail="Missing AMADEUS_CLIENT_ID or AMADEUS_CLIENT_SECRET",
        )

    try:
        ranked = fd.fetch_ranked_deals(
            origin=origin_code,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            top_destinations=top_destinations,
            probe_destinations=probe_destinations,
            probe_workers=probe_workers,
            adults=adults,
            nonstop=nonstop,
            max_price=max_price,
            base_url=base_url,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            client_id=client_id,
            client_secret=client_secret,
        )
    except fd.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"API request failed: {exc}") from exc
    except fd.RequestException as exc:
        raise HTTPException(
            status_code=502, detail=f"Network request failed: {exc}"
        ) from exc

    return {
        "origin": origin_code,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "count": len(ranked),
        "deals": [asdict(item) for item in ranked],
    }
