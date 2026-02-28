#!/usr/bin/env python3
"""
Flight Discountinator

Aggregate flight deals from CMH (or any origin), score destinations, and rank
the best discount opportunities.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

try:
    import requests
except ModuleNotFoundError:
    requests = None  # type: ignore[assignment]


if requests is not None:
    RequestException = requests.RequestException
    HTTPError = requests.HTTPError
else:
    class RequestException(Exception):
        pass


    class HTTPError(RequestException):
        pass


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def min_max_scale(value: float, min_value: float, max_value: float) -> float:
    if max_value <= min_value:
        return 0.5
    return (value - min_value) / (max_value - min_value)


def safe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def normalize_origin(origin: str) -> str:
    normalized = origin.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError("Origin must be a 3-letter IATA code.")
    return normalized


def resolve_date_window(
    start_date: Optional[str],
    end_date: Optional[str],
    lookahead_days: int,
) -> tuple[date, date]:
    if lookahead_days < 1:
        raise ValueError("lookahead_days must be >= 1.")

    if start_date:
        start = parse_iso_date(start_date)
    else:
        start = date.today() + timedelta(days=14)

    if end_date:
        end = parse_iso_date(end_date)
    else:
        end = start + timedelta(days=lookahead_days)

    if end <= start:
        raise ValueError("end_date must be after start_date.")

    return start, end


@dataclass
class FareCandidate:
    source: str
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str]
    currency: str
    total_price: float
    carrier: Optional[str] = None
    stops: Optional[int] = None


@dataclass
class RankedDeal:
    rank: int
    destination: str
    score: float
    best_price: float
    currency: str
    departure_date: str
    return_date: Optional[str]
    market_discount_pct: float
    consistency_pct: float
    option_depth_pct: float
    sample_count: int
    source_count: int
    sources: List[str]


class AmadeusClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        if requests is None:
            raise RuntimeError("The requests package is required for API calls.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(1, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, object]] = None,
        data: Optional[Dict[str, object]] = None,
        headers: Optional[Dict[str, str]] = None,
        require_auth: bool,
    ) -> Dict[str, object]:
        if requests is None:
            raise RuntimeError("The requests package is required for API calls.")

        request_headers: Dict[str, str] = {}
        if headers:
            request_headers.update(headers)
        if require_auth:
            self._ensure_token()
            if self._token is None:
                raise RuntimeError("Missing auth token")
            request_headers["Authorization"] = f"Bearer {self._token}"

        url = f"{self.base_url}{path}"
        last_exception: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    timeout=30,
                    headers=request_headers,
                )
                if (
                    response.status_code in RETRYABLE_STATUS_CODES
                    and attempt < self.max_retries
                ):
                    time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
                    continue

                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("Unexpected API response shape")
                return payload
            except requests.RequestException as exc:
                last_exception = exc
                if attempt >= self.max_retries:
                    raise
                time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))

        if last_exception is not None:
            raise last_exception
        raise RuntimeError("API request failed without exception details")

    def _authenticate(self) -> None:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        data = self._request_json(
            method="POST",
            path="/v1/security/oauth2/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            require_auth=False,
        )

        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 1200))
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    def _ensure_token(self) -> None:
        now = datetime.now(timezone.utc)
        if (
            self._token is None
            or self._token_expiry is None
            or now >= self._token_expiry - timedelta(minutes=2)
        ):
            self._authenticate()

    def _get(self, path: str, params: Dict[str, object]) -> Dict[str, object]:
        return self._request_json(
            method="GET",
            path=path,
            params=params,
            require_auth=True,
        )

    def search_inspiration(
        self,
        origin: str,
        start_date: str,
        end_date: str,
        max_price: Optional[float],
        nonstop: bool,
    ) -> List[FareCandidate]:
        params: Dict[str, object] = {
            "origin": origin,
            "departureDate": f"{start_date},{end_date}",
            "currencyCode": "USD",
            "viewBy": "DESTINATION",
        }
        if max_price is not None:
            params["maxPrice"] = int(max_price)
        if nonstop:
            params["nonStop"] = "true"

        payload = self._get("/v1/shopping/flight-destinations", params=params)
        raw_data = payload.get("data", [])
        if not isinstance(raw_data, list):
            return []

        deals: List[FareCandidate] = []
        for item in raw_data:
            if not isinstance(item, dict):
                continue
            price_block = item.get("price", {})
            if not isinstance(price_block, dict):
                continue

            total_price = safe_float(price_block.get("total"))
            if total_price is None:
                continue

            destination = item.get("destination")
            departure_date = item.get("departureDate")
            if not isinstance(destination, str) or not isinstance(departure_date, str):
                continue

            return_date = item.get("returnDate")
            if return_date is not None and not isinstance(return_date, str):
                return_date = None

            deals.append(
                FareCandidate(
                    source="amadeus_inspiration",
                    origin=origin,
                    destination=destination,
                    departure_date=departure_date,
                    return_date=return_date,
                    currency=str(price_block.get("currency", "USD")),
                    total_price=total_price,
                )
            )
        return deals

    def search_best_offer(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: Optional[str],
        adults: int,
        nonstop: bool,
    ) -> Optional[FareCandidate]:
        params: Dict[str, object] = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date,
            "adults": adults,
            "currencyCode": "USD",
            "max": 5,
        }
        if return_date:
            params["returnDate"] = return_date
        if nonstop:
            params["nonStop"] = "true"

        payload = self._get("/v2/shopping/flight-offers", params=params)
        raw_data = payload.get("data", [])
        if not isinstance(raw_data, list) or not raw_data:
            return None

        best_offer: Optional[dict] = None
        best_price = float("inf")
        for offer in raw_data:
            if not isinstance(offer, dict):
                continue
            price_block = offer.get("price", {})
            if not isinstance(price_block, dict):
                continue
            total = safe_float(price_block.get("total"))
            if total is None:
                continue
            if total < best_price:
                best_price = total
                best_offer = offer

        if best_offer is None:
            return None

        price_block = best_offer.get("price", {})
        if not isinstance(price_block, dict):
            return None
        total_price = safe_float(price_block.get("total"))
        if total_price is None:
            return None

        carrier: Optional[str] = None
        stops: Optional[int] = None

        itineraries = best_offer.get("itineraries")
        if isinstance(itineraries, list) and itineraries:
            first_itinerary = itineraries[0]
            if isinstance(first_itinerary, dict):
                segments = first_itinerary.get("segments")
                if isinstance(segments, list) and segments:
                    first_segment = segments[0]
                    if isinstance(first_segment, dict):
                        carrier_code = first_segment.get("carrierCode")
                        if isinstance(carrier_code, str):
                            carrier = carrier_code
                    stops = max(0, len(segments) - 1)

        return FareCandidate(
            source="amadeus_offers",
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            currency=str(price_block.get("currency", "USD")),
            total_price=total_price,
            carrier=carrier,
            stops=stops,
        )


class FlightDealAggregator:
    def __init__(self, client: AmadeusClient) -> None:
        self.client = client

    def collect(
        self,
        origin: str,
        start_date: str,
        end_date: str,
        max_price: Optional[float],
        adults: int,
        nonstop: bool,
        probe_destinations: int,
        probe_workers: int,
    ) -> List[FareCandidate]:
        inspiration = self.client.search_inspiration(
            origin=origin,
            start_date=start_date,
            end_date=end_date,
            max_price=max_price,
            nonstop=nonstop,
        )
        if not inspiration:
            return []

        inspiration_sorted = sorted(inspiration, key=lambda item: item.total_price)
        candidates: List[FareCandidate] = list(inspiration_sorted)

        seen_destinations = set()
        to_probe: List[FareCandidate] = []
        for candidate in inspiration_sorted:
            if candidate.destination in seen_destinations:
                continue
            seen_destinations.add(candidate.destination)
            to_probe.append(candidate)
            if len(to_probe) >= probe_destinations:
                break

        probe_workers = max(1, probe_workers)
        if probe_workers == 1:
            for base_deal in to_probe:
                try:
                    enriched = self.client.search_best_offer(
                        origin=origin,
                        destination=base_deal.destination,
                        departure_date=base_deal.departure_date,
                        return_date=base_deal.return_date,
                        adults=adults,
                        nonstop=nonstop,
                    )
                except RequestException as exc:
                    print(
                        f"[warn] Skipping offer check for {base_deal.destination}: {exc}"
                    )
                    continue
                if enriched is not None:
                    candidates.append(enriched)
        else:
            futures: Dict[concurrent.futures.Future, FareCandidate] = {}
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=probe_workers
            ) as executor:
                for base_deal in to_probe:
                    future = executor.submit(
                        self.client.search_best_offer,
                        origin,
                        base_deal.destination,
                        base_deal.departure_date,
                        base_deal.return_date,
                        adults,
                        nonstop,
                    )
                    futures[future] = base_deal

                enriched_results: List[FareCandidate] = []
                for future in concurrent.futures.as_completed(futures):
                    base_deal = futures[future]
                    try:
                        enriched = future.result()
                    except RequestException as exc:
                        print(
                            f"[warn] Skipping offer check for {base_deal.destination}: {exc}"
                        )
                        continue

                    if enriched is not None:
                        enriched_results.append(enriched)

                enriched_results.sort(key=lambda row: (row.destination, row.total_price))
                candidates.extend(enriched_results)

        deduped: Dict[str, FareCandidate] = {}
        for candidate in candidates:
            key = "|".join(
                [
                    candidate.source,
                    candidate.origin,
                    candidate.destination,
                    candidate.departure_date,
                    candidate.return_date or "",
                    candidate.currency,
                    f"{candidate.total_price:.2f}",
                ]
            )
            deduped[key] = candidate

        return sorted(
            deduped.values(),
            key=lambda row: (row.destination, row.total_price, row.source),
        )


def time_window_bonus(departure_date: str) -> float:
    try:
        days_out = (parse_iso_date(departure_date) - date.today()).days
    except ValueError:
        return 0.5

    if days_out < 0:
        return 0.25
    if days_out <= 30:
        return 1.0
    if days_out <= 90:
        return 0.75
    if days_out <= 180:
        return 0.6
    return 0.4


def option_depth_score(sample_count: int) -> float:
    # Mild bonus for destinations that appear repeatedly in the pulled data.
    return clamp((sample_count - 1) / 4.0)


def rank_destinations(candidates: List[FareCandidate], top_n: int) -> List[RankedDeal]:
    if not candidates:
        return []

    grouped: Dict[str, List[FareCandidate]] = {}
    for item in candidates:
        grouped.setdefault(item.destination, []).append(item)

    best_by_destination: Dict[str, FareCandidate] = {}
    for destination, fares in grouped.items():
        best_by_destination[destination] = min(fares, key=lambda f: f.total_price)

    all_best_prices = [deal.total_price for deal in best_by_destination.values()]

    min_best_price = min(all_best_prices)
    max_best_price = max(all_best_prices)
    market_median = statistics.median(all_best_prices)

    provisional: List[RankedDeal] = []
    for destination, fares in grouped.items():
        prices = [item.total_price for item in fares]
        best = min(fares, key=lambda f: f.total_price)

        source_best_prices: Dict[str, float] = {}
        for fare in fares:
            existing = source_best_prices.get(fare.source)
            if existing is None or fare.total_price < existing:
                source_best_prices[fare.source] = fare.total_price
        per_source_prices = list(source_best_prices.values())

        if len(per_source_prices) > 1:
            spread_pct = (max(per_source_prices) - min(per_source_prices)) / max(
                per_source_prices
            )
            consistency = clamp(1.0 - spread_pct)
        else:
            consistency = 0.65

        cheapness = 1.0 - min_max_scale(best.total_price, min_best_price, max_best_price)
        market_discount = (
            (market_median - best.total_price) / market_median if market_median > 0 else 0.0
        )
        market_discount = clamp(market_discount)

        sources = sorted(source_best_prices.keys())
        source_confidence = min(1.0, len(per_source_prices) / 2.0)

        bonus = time_window_bonus(best.departure_date)
        option_depth = option_depth_score(len(prices))

        score = 100.0 * (
            0.35 * cheapness
            + 0.30 * market_discount
            + 0.15 * consistency
            + 0.10 * source_confidence
            + 0.05 * bonus
            + 0.05 * option_depth
        )

        provisional.append(
            RankedDeal(
                rank=0,
                destination=destination,
                score=round(score, 2),
                best_price=round(best.total_price, 2),
                currency=best.currency,
                departure_date=best.departure_date,
                return_date=best.return_date,
                market_discount_pct=round(market_discount * 100.0, 2),
                consistency_pct=round(consistency * 100.0, 2),
                option_depth_pct=round(option_depth * 100.0, 2),
                sample_count=len(prices),
                source_count=len(sources),
                sources=sources,
            )
        )

    ranked = sorted(
        provisional,
        key=lambda row: (-row.score, row.best_price, row.destination),
    )[:top_n]

    for idx, deal in enumerate(ranked, start=1):
        deal.rank = idx

    return ranked


def fetch_ranked_deals(
    origin: str,
    start_date: str,
    end_date: str,
    top_destinations: int,
    probe_destinations: int,
    probe_workers: int,
    adults: int,
    nonstop: bool,
    max_price: Optional[float],
    base_url: str,
    max_retries: int,
    retry_backoff: float,
    client_id: str,
    client_secret: str,
) -> List[RankedDeal]:
    client = AmadeusClient(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff,
    )
    aggregator = FlightDealAggregator(client)
    candidates = aggregator.collect(
        origin=origin,
        start_date=start_date,
        end_date=end_date,
        max_price=max_price,
        adults=adults,
        nonstop=nonstop,
        probe_destinations=probe_destinations,
        probe_workers=probe_workers,
    )
    return rank_destinations(candidates=candidates, top_n=top_destinations)


def print_table(deals: List[RankedDeal]) -> None:
    if not deals:
        print("No ranked destinations to display.")
        return

    headers = [
        "Rank",
        "Dest",
        "Score",
        "Best Price",
        "Departs",
        "Returns",
        "Mkt Disc",
        "Depth",
        "Samples",
        "Sources",
    ]
    rows = []
    for deal in deals:
        rows.append(
            [
                str(deal.rank),
                deal.destination,
                f"{deal.score:.2f}",
                f"{deal.currency} {deal.best_price:.2f}",
                deal.departure_date,
                deal.return_date or "-",
                f"{deal.market_discount_pct:.1f}%",
                f"{deal.option_depth_pct:.1f}%",
                str(deal.sample_count),
                ",".join(deal.sources),
            ]
        )

    widths = []
    for col_index in range(len(headers)):
        widths.append(max(len(headers[col_index]), *(len(row[col_index]) for row in rows)))

    line = " | ".join(headers[idx].ljust(widths[idx]) for idx in range(len(headers)))
    divider = "-+-".join("-" * widths[idx] for idx in range(len(headers)))
    print(line)
    print(divider)
    for row in rows:
        print(" | ".join(row[idx].ljust(widths[idx]) for idx in range(len(headers))))


def write_json(path: str, deals: List[RankedDeal]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([asdict(deal) for deal in deals], handle, indent=2)


def write_csv(path: str, deals: List[RankedDeal]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "rank",
                "destination",
                "score",
                "best_price",
                "currency",
                "departure_date",
                "return_date",
                "market_discount_pct",
                "consistency_pct",
                "option_depth_pct",
                "sample_count",
                "source_count",
                "sources",
            ]
        )
        for deal in deals:
            writer.writerow(
                [
                    deal.rank,
                    deal.destination,
                    f"{deal.score:.2f}",
                    f"{deal.best_price:.2f}",
                    deal.currency,
                    deal.departure_date,
                    deal.return_date or "",
                    f"{deal.market_discount_pct:.2f}",
                    f"{deal.consistency_pct:.2f}",
                    f"{deal.option_depth_pct:.2f}",
                    deal.sample_count,
                    deal.source_count,
                    ";".join(deal.sources),
                ]
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate and score discounted flight destinations from an origin airport."
    )
    parser.add_argument("--origin", default="CMH", help="Origin airport IATA code.")
    parser.add_argument(
        "--start-date",
        help="Earliest departure date (YYYY-MM-DD). Defaults to today + 14 days.",
    )
    parser.add_argument(
        "--end-date",
        help="Latest departure date (YYYY-MM-DD). Defaults to start date + 120 days.",
    )
    parser.add_argument(
        "--lookahead-days",
        type=int,
        default=120,
        help="Used when --end-date is omitted.",
    )
    parser.add_argument(
        "--top-destinations",
        type=int,
        default=10,
        help="Number of ranked destinations to show.",
    )
    parser.add_argument(
        "--probe-destinations",
        type=int,
        default=20,
        help="How many destinations to enrich with second-source live offers.",
    )
    parser.add_argument(
        "--probe-workers",
        type=int,
        default=4,
        help="Parallel workers for live offer enrichment (use 1 for serial).",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=None,
        help="Optional max USD fare filter.",
    )
    parser.add_argument("--adults", type=int, default=1, help="Traveler count.")
    parser.add_argument(
        "--nonstop",
        action="store_true",
        help="Only include nonstop options.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("AMADEUS_BASE_URL", "https://test.api.amadeus.com"),
        help="Amadeus API base URL.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retry attempts for transient API errors.",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=1.0,
        help="Base seconds for exponential retry backoff.",
    )
    parser.add_argument(
        "--output-json",
        default="deals_ranked.json",
        help="Output JSON file.",
    )
    parser.add_argument(
        "--output-csv",
        default="deals_ranked.csv",
        help="Output CSV file.",
    )
    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    if requests is None:
        print("Missing dependency: requests")
        print("Install it with: pip install -r requirements.txt")
        return 2

    try:
        origin = normalize_origin(args.origin)
    except ValueError as exc:
        parser.error(f"--origin invalid: {exc}")

    try:
        start, end = resolve_date_window(
            start_date=args.start_date,
            end_date=args.end_date,
            lookahead_days=args.lookahead_days,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.top_destinations < 1:
        parser.error("--top-destinations must be >= 1.")
    if args.probe_destinations < 1:
        parser.error("--probe-destinations must be >= 1.")
    if args.probe_workers < 1:
        parser.error("--probe-workers must be >= 1.")
    if args.adults < 1:
        parser.error("--adults must be >= 1.")
    if args.max_retries < 1:
        parser.error("--max-retries must be >= 1.")
    if args.retry_backoff < 0:
        parser.error("--retry-backoff must be >= 0.")

    client_id = os.getenv("AMADEUS_CLIENT_ID")
    client_secret = os.getenv("AMADEUS_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Missing AMADEUS_CLIENT_ID or AMADEUS_CLIENT_SECRET in environment.")
        print("Use .env.example as a template.")
        return 2

    try:
        ranked = fetch_ranked_deals(
            origin=origin,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            top_destinations=args.top_destinations,
            probe_destinations=args.probe_destinations,
            probe_workers=args.probe_workers,
            adults=args.adults,
            nonstop=args.nonstop,
            max_price=args.max_price,
            base_url=args.base_url,
            max_retries=args.max_retries,
            retry_backoff=args.retry_backoff,
            client_id=client_id,
            client_secret=client_secret,
        )
    except HTTPError as exc:
        print(f"API request failed: {exc}")
        return 1
    except RequestException as exc:
        print(f"Network request failed: {exc}")
        return 1

    if not ranked:
        print("No ranked results available.")
        return 0

    print()
    print(
        f"Flight Discountinator ranking from {origin} "
        f"({start.isoformat()} to {end.isoformat()})"
    )
    print_table(ranked)
    print()

    write_json(args.output_json, ranked)
    write_csv(args.output_csv, ranked)
    print(f"Saved JSON: {args.output_json}")
    print(f"Saved CSV : {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
