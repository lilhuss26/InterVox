from io import BytesIO

import pytest


def _pdf():
    return (BytesIO(b"%PDF-1.4 fake"), "resume.pdf")


def test_start_requires_resume(client):
    resp = client.post(
        "/api/interview/start",
        data={"job_description": "Backend engineer"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "resume and job_description are required"}


def test_start_requires_job_description(client):
    resp = client.post(
        "/api/interview/start",
        data={"resume": _pdf()},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "resume and job_description are required"}


@pytest.mark.parametrize(
    "raw, expected",
    [("abc", 5), ("1", 3), ("99", 15), (None, 5), ("7", 7)],
)
def test_start_num_questions_clamping(client, monkeypatch, raw, expected):
    seen = {}

    def fake_start_interview(pdf_file, job_description, num_questions=5):
        seen["num_questions"] = num_questions
        return {"session_id": "sid", "first_question": "Q?"}

    # Patch the ROUTER's binding, not the service module's. The router does
    # `from src.api.services.interview import start_interview` at import time, so
    # it holds its own reference; patching the service module would leave the
    # real (network-hitting) function in place and the test would pass silently.
    monkeypatch.setattr(
        "src.api.routers.interview.start_interview", fake_start_interview
    )

    data = {"resume": _pdf(), "job_description": "Backend engineer"}
    if raw is not None:
        data["num_questions"] = raw

    resp = client.post(
        "/api/interview/start", data=data, content_type="multipart/form-data"
    )

    assert resp.status_code == 200
    assert seen["num_questions"] == expected
    assert resp.get_json() == {"session_id": "sid", "first_question": "Q?"}


def test_report_404_unknown_session(client):
    resp = client.get("/api/interview/does-not-exist/report")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "session not found"}


def test_get_model_defaults_to_haiku(client):
    resp = client.get("/api/interview/model")
    assert resp.status_code == 200
    assert resp.get_json() == {"model": "haiku", "available_models": ["haiku", "sonnet"]}


def test_set_model_switches_selection(client):
    resp = client.post("/api/interview/model", json={"model": "sonnet"})
    assert resp.status_code == 200
    assert resp.get_json() == {"model": "sonnet", "available_models": ["haiku", "sonnet"]}

    resp = client.get("/api/interview/model")
    assert resp.get_json()["model"] == "sonnet"


def test_set_model_rejects_unknown_model(client):
    resp = client.post("/api/interview/model", json={"model": "gpt-4"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_start_interview_uses_selected_model(client, monkeypatch):
    seen = {}

    def fake_start_interview(pdf_file, job_description, num_questions=5):
        from src.api.model_store import get_selected_model_name

        seen["model"] = get_selected_model_name()
        return {"session_id": "sid", "first_question": "Q?"}

    monkeypatch.setattr(
        "src.api.routers.interview.start_interview", fake_start_interview
    )

    client.post("/api/interview/model", json={"model": "sonnet"})

    resp = client.post(
        "/api/interview/start",
        data={"resume": _pdf(), "job_description": "Backend engineer"},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200
    assert seen["model"] == "sonnet"
