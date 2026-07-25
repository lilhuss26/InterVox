import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# agent/config/llm.py builds ChatAnthropic at IMPORT time, and it rejects a
# None api_key. Seed a dummy before any test module pulls that in. setdefault,
# not assignment, so a real local .env still wins (load_dotenv defaults to
# override=False). No test ever calls the model — they all inject FakeLLM.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from agent.models import (  # noqa: E402
    InterviewPlan,
    InterviewQuestion,
    JobDescription,
    ResumeDetails,
)


class _FakeStructured:
    def __init__(self, parent, schema):
        self._parent = parent
        self._schema = schema

    def invoke(self, messages):
        self._parent.calls.append((self._schema, messages))
        return self._parent.responses[self._schema]


class FakeLLM:
    """Stand-in for a LangChain chat model.

    Implements only what the agents actually use: with_structured_output(Model)
    returning something with .invoke(messages). Responses are keyed by schema
    class so one FakeLLM can serve several agents, and every call is recorded
    on .calls for assertions.
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        if schema not in self.responses:
            raise AssertionError(f"FakeLLM has no canned response for {schema!r}")
        return _FakeStructured(self, schema)


@pytest.fixture
def fake_llm():
    return FakeLLM


@pytest.fixture
def make_question():
    def _make(qid="q1", text="Tell me about indexes.", difficulty="medium"):
        return InterviewQuestion(
            id=qid,
            section="technical",
            question_text=text,
            difficulty=difficulty,
            target_skills=["sql"],
            is_planned=True,
        )

    return _make


@pytest.fixture
def make_plan(make_question):
    def _make(n=3, estimated=None):
        # estimated is decoupled from n on purpose: the planner overwrites the
        # count field without touching the list, and several tests hinge on that.
        questions = [make_question(qid=f"q{i}", text=f"Question {i}?") for i in range(n)]
        return InterviewPlan(
            sections=["technical"],
            planned_questions=questions,
            scoring_dimensions=["depth"],
            difficulty_progression=["easy", "medium", "hard"],
            target_skills=["sql"],
            estimated_question_count=n if estimated is None else estimated,
        )

    return _make


@pytest.fixture
def resume_data():
    return ResumeDetails(
        candidate_name="Ada",
        years_experience=4,
        skills=["python"],
        projects=["compiler"],
        education=["BSc"],
        experience_summary="Backend work.",
        strength_areas=["algorithms"],
        weak_areas=["frontend"],
        certifications=[],
        technologies=["postgres"],
    )


@pytest.fixture
def job_data():
    return JobDescription(
        role_title="Backend Engineer",
        required_skills=["python"],
        preferred_skills=["go"],
        seniority="mid",
        responsibilities=["ship services"],
        keywords=["api"],
        interview_focus=["system design"],
        must_have_skills=["python"],
    )


@pytest.fixture
def app():
    from src.api.app import create_app

    flask_app = create_app()
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def clean_session_store():
    # session_store is a module-level dict, so state leaks across tests.
    from src.api.session_store import session_store

    session_store.clear()
    yield
    session_store.clear()


@pytest.fixture(autouse=True)
def reset_model_selection():
    # model_store tracks the selected model in a module-level dict, so a
    # switch in one test would otherwise leak into the next.
    from agent.config.llm import DEFAULT_MODEL
    from src.api.model_store import set_selected_model_name

    set_selected_model_name(DEFAULT_MODEL)
    yield
    set_selected_model_name(DEFAULT_MODEL)


# --------------------------------------------------------------------------
# Pipeline (email -> GitHub issue) doubles.
#
# Same philosophy as FakeLLM above: hand-rolled stubs implementing only the
# surface the code actually touches, recording every call for assertions, and
# raising loudly on anything unstubbed rather than returning a silent None.
# --------------------------------------------------------------------------


class _FakeRequest:
    """Stands in for a googleapiclient request object (has .execute())."""

    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeHistoryApi:
    def __init__(self, parent):
        self._parent = parent
        self._index = 0

    def list(self, **kwargs):
        self._parent.calls.append(("history.list", kwargs))
        if self._parent.history_error is not None:
            return _FakeRequest(self._parent.history_error)
        self._index = 0
        pages = self._parent.history_pages
        return _FakeRequest(pages[0] if pages else {"history": []})

    def list_next(self, request, page):
        self._index += 1
        if self._index >= len(self._parent.history_pages):
            return None
        return _FakeRequest(self._parent.history_pages[self._index])


class _FakeMessagesApi:
    def __init__(self, parent):
        self._parent = parent

    def get(self, userId=None, id=None, format=None):
        self._parent.calls.append(("messages.get", id))
        if id not in self._parent.canned_messages:
            raise AssertionError(f"FakeGmail has no canned message for {id!r}")
        return _FakeRequest(self._parent.canned_messages[id])


class FakeGmail:
    """Mimics the chained builder surface, as far as the bridge walks it:

        users().history().list(...).execute()
        users().history().list_next(request, page)
        users().messages().get(...).execute()
        users().getProfile(...).execute()

    history() returns the SAME sub-object each time, because pagination state
    lives on it — the bridge calls users().history().list_next(...) on a fresh
    chain and must still advance.
    """

    def __init__(
        self,
        history_pages=None,
        messages=None,
        profile_history_id=999,
        history_error=None,
    ):
        self.history_pages = list(history_pages or [])
        # Named canned_messages, not messages: an instance attribute by that
        # name would shadow the messages() builder method below.
        self.canned_messages = dict(messages or {})
        self.profile_history_id = profile_history_id
        self.history_error = history_error
        self.calls = []
        self._history_api = _FakeHistoryApi(self)
        self._messages_api = _FakeMessagesApi(self)

    def users(self):
        return self

    def history(self):
        return self._history_api

    def messages(self):
        return self._messages_api

    def getProfile(self, userId=None):
        return _FakeRequest({"historyId": self.profile_history_id})


class FakeIssue:
    def __init__(self, number, title, body, labels):
        self.number = number
        self.title = title
        self.body = body
        self.labels = labels


class FakeUnknownObject(Exception):
    """Stand-in for github.UnknownObjectException."""


class FakeRepo:
    def __init__(self, start=1, existing_issues=None, raise_on_create=None):
        self.created = []
        self.labels = {"auto-task"}
        self._next = start
        self.existing_issues = list(existing_issues or [])
        self.raise_on_create = raise_on_create

    def get_label(self, name):
        if name not in self.labels:
            raise FakeUnknownObject(name)
        return name

    def create_label(self, name, color):
        self.labels.add(name)
        return name

    def get_issues(self, state=None, labels=None):
        return list(self.existing_issues)

    def create_issue(self, title, body, labels):
        if self.raise_on_create:
            raise self.raise_on_create
        issue = FakeIssue(self._next, title, body, labels)
        self._next += 1
        self.created.append(issue)
        return issue


@pytest.fixture
def fake_gmail():
    return FakeGmail


@pytest.fixture
def fake_repo():
    return FakeRepo


@pytest.fixture
def pipeline_settings(tmp_path, monkeypatch):
    """Isolated Settings pointing DB_PATH at a temp file.

    get_settings is lru_cached, so the cache must be cleared on both sides or
    the first test to call it pins the DB path for the whole session.
    """
    from pipeline import config

    monkeypatch.setenv("DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setenv("ALLOWLIST", "h.m.elnemr@gmail.com,mohamed.ramadan@aman.eg")
    monkeypatch.setenv("GITHUB_REPO", "lilhuss26/voice_interview_agent")
    monkeypatch.setenv("WEBHOOK_AUDIENCE", "https://pipeline.example/gmail/webhook")
    monkeypatch.setenv("PUBSUB_SA_EMAIL", "")
    config.get_settings.cache_clear()
    settings = config.get_settings()

    from pipeline.storage import db

    db.init_db(settings)
    yield settings
    config.get_settings.cache_clear()


@pytest.fixture
def make_raw_message():
    """Build a Gmail API message dict, base64url-encoded and UNPADDED.

    Unpadded on purpose: real Gmail strips the '=' padding, so fixtures that
    include it would hide a decoder bug rather than catch one.
    """
    import base64

    def _encode(text):
        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    def _make(
        mid="m1",
        subject="[TASK] Add a health check",
        sender="h.m.elnemr@gmail.com",
        body="Please add a /health endpoint.",
        mime="text/plain",
    ):
        return {
            "id": mid,
            "payload": {
                "mimeType": mime,
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": sender},
                ],
                "body": {"data": _encode(body)},
            },
        }

    return _make
