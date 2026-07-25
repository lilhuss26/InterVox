# Issue #20 — Add Sonnet model support alongside Haiku with user selection

## Scope

- **ai** (`agent/config/llm.py`) — add the Sonnet 3.5 model definition and a
  model registry.
- **back** (`src/`) — add server-side model-selection state, an API endpoint
  to switch models, and wire the interview service to use the selection
  instead of a hardcoded import.
- **front** (`ui/`) — add a model selector control to the start form and wire
  it to the new endpoint.

## Files to modify

- `agent/config/llm.py` — add `sonnet`, plus a `MODELS` registry dict and
  `DEFAULT_MODEL` name. Keep `haiku` exported as-is (`showcase.py`, outside
  this issue's scope, imports it directly).
- `src/api/model_store.py` (new) — in-memory selected-model state with
  getter/setter, mirroring the existing `session_store.py` pattern.
- `src/api/DTOs.py` — add `ModelSelectionRequest` / `ModelSelectionResponse`
  schemas, following the existing schema conventions.
- `src/api/routers/interview.py` — add `GET /api/interview/model` (read
  current selection + available models) and `POST /api/interview/model`
  (switch selection).
- `src/api/services/interview.py` — replace the hardcoded
  `from agent.config.llm import haiku` import with a lookup through
  `model_store.get_selected_model()`.
- `ui/index.html` — add a model `<select>` field to the start form, before
  the "Start live interview" button.
- `ui/app.js` — on load, fetch the current model selection and populate the
  selector; on change, POST the new selection to the endpoint before the
  interview starts.
- `ui/styles.css` — minor style so the new `<select>` matches the existing
  `.field` inputs.
- `tests/test_api.py` — cover the new endpoint (get default, switch to
  sonnet, reject unknown model) and confirm `start_interview` uses the
  selected model.
- `tests/test_dtos.py` — cover the new schemas.

## Implementation plan

- `agent/config/llm.py` gets a second `ChatAnthropic` instance, `sonnet`,
  using model id `claude-3-5-sonnet-20241022`, and a `MODELS` dict
  (`{"haiku": haiku, "sonnet": sonnet}`) plus `DEFAULT_MODEL = "haiku"`, so
  callers can resolve a model by name without hardcoding an import.
- `src/api/model_store.py` holds a single module-level dict (same shape as
  `session_store.py`) tracking the currently-selected model name, defaulting
  to `DEFAULT_MODEL`. It exposes `get_selected_model_name()`,
  `set_selected_model_name(name)` (raises `ValueError` on an unknown name),
  and `get_selected_model()` (resolves the name through `MODELS`).
- New DTOs mirror the existing marshmallow style:
  `ModelSelectionRequest.model` (required `Str`),
  `ModelSelectionResponse.model` (`Str`) and `.available_models`
  (`List(Str)`).
- New router endpoints on the existing `interview_bp`:
  - `GET /api/interview/model` → `{"model": <current>, "available_models": [...]}`.
  - `POST /api/interview/model` with JSON body `{"model": "haiku"|"sonnet"}`
    → validates against `MODELS`, calls `set_selected_model_name`, returns
    the same shape as GET. Invalid/missing model → 400 with an `error` key,
    consistent with the existing `/start` validation style.
- `src/api/services/interview.py: start_interview` calls
  `model_store.get_selected_model()` at call time (not import time) so a
  selection made via the endpoint takes effect on the next interview.
- UI: add a labeled `<select id="modelSelect">` with `Haiku`/`Sonnet 3.5`
  options to the start form in `index.html`. In `app.js`, fetch
  `GET /api/interview/model` on page load to populate/select the current
  value, and on the select's `change` event POST the new value to
  `POST /api/interview/model` (best-effort; surface an error toast on
  failure, matching the existing `showError` pattern). This satisfies "user
  selects model before question generation" without changing the
  `/start` request shape.

## Tasks

1. Add `sonnet` + `MODELS` + `DEFAULT_MODEL` to `agent/config/llm.py`.
2. Add `src/api/model_store.py`.
3. Add `ModelSelectionRequest` / `ModelSelectionResponse` to `src/api/DTOs.py`.
4. Add `GET`/`POST /api/interview/model` routes to
   `src/api/routers/interview.py`.
5. Update `src/api/services/interview.py` to use
   `model_store.get_selected_model()` instead of the hardcoded `haiku`
   import.
6. Update `ui/index.html`, `ui/app.js`, `ui/styles.css` for the selector.
7. Add/update tests in `tests/test_api.py` and `tests/test_dtos.py`.
8. Run `pytest` and confirm green.
9. Commit code + this plan doc, open the PR per the output contract.
