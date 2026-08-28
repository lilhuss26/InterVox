# Issue #14: Add /ping health endpoint

## Scope

Back (`src/`) — Flask app route registration.

## Files to modify

- `src/api/app.py` — register a new `GET /ping` route.
- `tests/test_api.py` — add a test covering the new endpoint.

## Implementation plan

Add a `/ping` route directly in `create_app()` in `src/api/app.py`, alongside
the existing `/` index route. It takes no parameters and has no dependency on
session state or blueprints, so a plain `@app.route("/ping")` matches the
existing pattern used for `/`. It returns `jsonify({"pong": True})` with the
default 200 status code, satisfying both acceptance criteria.

## Tasks

1. Add the `/ping` route to `src/api/app.py`.
2. Add a test in `tests/test_api.py` asserting `GET /ping` returns status 200
   and JSON body `{"pong": true}`.
3. Run `pytest` and confirm the full suite is green.
4. Commit the code change and this plan doc, open the PR per the output
   contract.
