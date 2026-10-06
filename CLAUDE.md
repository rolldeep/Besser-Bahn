# Besser-Bahn

Premium Deutsche-Bahn companion app. Flutter app in `flutter-app/`, self-hosted
delay-prediction API in `prediction-service/`, API probes in `api-tests/`,
ticket search/book CLI + cron last-minute fare watcher in `ticket-tool/`.

## Workflow — ALWAYS commit & push after changes

After every change that leaves the working tree in a good state (code compiles /
`flutter analyze` clean), **commit and push immediately**:

```
git add -A && git commit -m "<conventional message>" && git push
```

- Use Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`…).
- Don't wait to batch unrelated work — commit each logical change as it lands.
- **Never commit secrets**: Android signing keys / upload key
  (`android/*.jks`, `*.keystore`, `encrypted_upload_key.zip`,
  `upload_certificate.pem`, `pepk.jar`), `key.properties`. These are gitignored —
  keep them that way.

## Network / endpoints — health-check discipline

`api-tests/healthcheck.py` probes every upstream endpoint the app depends on and
asserts the response still has the shape we parse.

- **At the start of every session that touches network/data code, run it first**
  (`cd api-tests && python3 healthcheck.py`). If a hard check fails, an upstream
  endpoint changed — investigate and switch/fix the endpoint *before* doing other
  work, don't build on a broken API.
- **Whenever you add or change networking** (a new endpoint, media type, request
  shape, or response parsing), **add or extend a check in `healthcheck.py`** to
  cover it, and run the script to confirm it passes.
- Soft checks (`soft=True`) warn instead of fail — use for known-flaky sources
  (e.g. the public HAFAS mirror).

## Repo hygiene

Only Flutter app code, the prediction service, api-tests, ticket-tool, and docs
belong in the repo. Do **not** commit dev artifacts: screenshots (`screenshot-*.png`), tool
caches (`.playwright-mcp/`), signing keys. They're gitignored — keep them out.

## Architecture notes

- Data layer prefers the **DB Vendo backend** (`app.services-bahn.de/mob`, the
  DB Navigator app's API) over the bahn.de website (Akamai-blocked) and the
  public HAFAS mirror (`v6.db.transport.rest`, unreliable/down).
- Route map geometry comes from `GET /mob/zuglauf/{id}` (`zuglauf.v2+json`) —
  the exact track polyline DB draws on its own map. Cached per physical route in
  `lib/core/polyline_cache.dart`.
- `ticket-tool/` (Python, `bbt`) has its own /mob client in
  `bbtickets/vendo.py`; `healthcheck.py`'s `check_ticket_tool` runs that parser
  live. Offline tests: `cd ticket-tool && uv run python -m unittest discover -s tests`
  (or `python3 -m …`). `pyproject.toml` + `uv.lock` back `uv run bbt` / `uv tool install`.
- `bbt agent` (`bbtickets/agent.py`, extra `[agent]`): Claude Agent SDK agent whose
  only tools are `bbtickets/agent_tools.py` (plain, SDK-free, tested offline);
  BahnCard 25 by default; Langfuse tracing via OpenInference when
  `LANGFUSE_*` keys are set. Fare logic shared with `hunt` lives in `deals.py`.
- `bbt mcp` (`bbtickets/mcp_server.py`, extra `[mcp]`): the same tools + `GUIDE`
  as an MCP server (stdio, or streamable HTTP with a bearer token) so LibreChat
  runs the agent. Config in `ticket-tool/librechat/`, image from
  `ticket-tool/Dockerfile`. Keep `librechat.yaml` in sync with `agent.TOOLS` /
  `DEFAULT_MODEL` (`tests/test_mcp.py` checks it).
- New app structure lives under `flutter-app/lib/screens/`,
  `providers/` (Riverpod), `router/` (GoRouter), `services/`.
