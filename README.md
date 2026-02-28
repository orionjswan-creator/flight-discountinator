# Flight Discountinator

Aggregate flight prices from `CMH` and rank top destinations by a discount score.
Includes both a CLI and a deployable HTTP API.

## What It Does

- Pulls destination deals from Amadeus Flight Inspiration Search.
- Enriches each destination with a second price source using Flight Offers Search.
- Scores destinations with a weighted discount model.
- Prints ranked results and writes JSON/CSV outputs.

## Scoring Model

Each destination gets a 0-100 score based on:

- `cheapness` (35%): lower fare vs all destinations.
- `market_discount` (30%): savings vs median of each destination's best fare.
- `consistency` (15%): low spread between each source's best price.
- `source_confidence` (10%): higher confidence when multiple sources agree.
- `time_window_bonus` (5%): bonus for deals departing in useful near-term windows.
- `option_depth` (5%): small bonus when a destination appears often in pulled data.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies.
3. Set Amadeus credentials.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:AMADEUS_CLIENT_ID = "your_client_id"
$env:AMADEUS_CLIENT_SECRET = "your_client_secret"
```

Amadeus developer account:
https://developers.amadeus.com/

Optional local `.env` file is auto-loaded (see `.env.example`).

## Run

```powershell
python flight_discountinator.py --origin CMH --top-destinations 12 --nonstop
```

Useful flags:

- `--start-date YYYY-MM-DD`
- `--end-date YYYY-MM-DD`
- `--max-price 350`
- `--probe-destinations 24`
- `--probe-workers 4`
- `--output-json deals.json`
- `--output-csv deals.csv`
- `--base-url https://test.api.amadeus.com`
- `--max-retries 3`
- `--retry-backoff 1.0`

## Outputs

- Console ranking table.
- JSON file (default: `deals_ranked.json`)
- CSV file (default: `deals_ranked.csv`)

## API (Local)

Run a local API server:

```powershell
uvicorn api:app --host 0.0.0.0 --port 8000
```

Or one-command local deploy (installs deps, restarts port, verifies health):

```powershell
.\deploy_local.ps1 -Port 8000
```

Endpoints:

- `GET /health`
- `GET /deals?origin=CMH&top_destinations=10&nonstop=true`

Example:

```powershell
curl "http://127.0.0.1:8000/deals?origin=CMH&top_destinations=8"
```

## Deploy (Docker)

Build image:

```powershell
docker build -t flight-discountinator:latest .
```

Run container:

```powershell
docker run --rm -p 8000:8000 `
  -e AMADEUS_CLIENT_ID=your_client_id `
  -e AMADEUS_CLIENT_SECRET=your_client_secret `
  flight-discountinator:latest
```

Then call:

```powershell
curl "http://127.0.0.1:8000/deals?origin=CMH&top_destinations=10"
```

## Deploy (Render)

- `render.yaml` is included for one-click Docker deploy.
- This repo pins Render to `plan: free` in `render.yaml` to avoid paid instance defaults.
- Set `AMADEUS_CLIENT_ID` and `AMADEUS_CLIENT_SECRET` in Render service env vars.
- Optional: set `AMADEUS_BASE_URL` to production API base URL.

## Audit Commands

```powershell
python flight_discountinator.py --help
python -m compileall api.py flight_discountinator.py tests
python -m unittest discover -s tests -v
```
