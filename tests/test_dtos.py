import pytest
from marshmallow import ValidationError

from src.api.DTOs import (
    InterviewStartRequest,
    ModelSelectionRequest,
    ModelSelectionResponse,
    ReportResponse,
    StartInterviewResponse,
)


def test_start_request_requires_job_description():
    with pytest.raises(ValidationError) as exc:
        InterviewStartRequest().load({})
    assert "job_description" in exc.value.messages


def test_start_response_drops_unknown_keys():
    # Load-bearing: start_interview() returns a dict that also holds the live
    # graph object, and this schema is what stops internals reaching the client.
    dumped = StartInterviewResponse().dump(
        {"session_id": "abc", "first_question": "Q?", "graph": object()}
    )
    assert dumped == {"session_id": "abc", "first_question": "Q?"}


def test_report_response_dump():
    dumped = ReportResponse().dump(
        {"final_report": {"score": 8}, "coaching_notes": {"tip": "x"}, "extra": 1}
    )
    assert dumped == {"final_report": {"score": 8}, "coaching_notes": {"tip": "x"}}


def test_model_selection_request_requires_model():
    with pytest.raises(ValidationError) as exc:
        ModelSelectionRequest().load({})
    assert "model" in exc.value.messages


def test_model_selection_response_dump():
    dumped = ModelSelectionResponse().dump(
        {"model": "sonnet", "available_models": ["haiku", "sonnet"], "extra": 1}
    )
    assert dumped == {"model": "sonnet", "available_models": ["haiku", "sonnet"]}
