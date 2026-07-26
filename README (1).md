# GA5 Incident Agent

## Run locally
```
pip install -r requirements.txt
export GEMINI_API_KEY=your_key_here
uvicorn app:app --host 0.0.0.0 --port 8000
```

Get a free Gemini API key at https://aistudio.google.com/apikey

## Test locally
```
curl -X POST http://localhost:8000/v2/incidents \
  -H "Content-Type: application/json" \
  -d @sample_request.json
```

## Deploy to Fly.io (free, always-on)
1. Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
2. `fly launch` (creates fly.toml, pick smallest free-tier VM, no Postgres needed)
3. `fly secrets set GEMINI_API_KEY=your_key_here`
4. Create a volume for the SQLite file so state survives restarts:
   `fly volumes create incident_data --size 1`
   Mount it in fly.toml under `[mounts]` to `/data`, then set
   `INCIDENT_DB_PATH=/data/incidents.db` as another secret/env var.
5. `fly deploy`
6. Your public HTTPS URL is `https://<app-name>.fly.dev`

## Known gaps to close before grading (see chat for details)
- Verify the exact outcome/approval receipt request shape against the real
  grader payloads once you have sandbox access - field names were inferred
  from the spec screenshots.
- Tune model.py's prompt if Gemini's tool-name/argument choices don't match
  the catalog's inputSchema closely enough - add few-shot examples if needed.
- Confirm resultClass string values and whether the grader wants any of them
  treated as a "failed but not timeout" case (e.g. explicit 4xx/5xx besides
  503) - default here folds anything non-200/non-503/non-timeout into
  "failed".
- Double check whether replayed traceparent/tracestate needs exact
  byte-for-byte preservation - store tracestate through untouched if you add
  that header.
