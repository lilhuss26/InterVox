import base64
import json

import pytest

from pipeline.bridge import Bridge
from pipeline.models import TaskClassification
from pipeline.storage import db

REPO = "lilhuss26/voice_interview_agent"


def envelope(email="hassom20042019@gmail.com", history_id=42):
    data = json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    return {"message": {"data": base64.urlsafe_b64encode(data).decode()}}


def classification(**overrides):
    fields = {
        "project": REPO,
        "actionable": True,
        "title": "Add a health check",
        "description": "Expose /health returning ok.",
        "acceptance": ["GET /health returns 200"],
    }
    fields.update(overrides)
    return TaskClassification(**fields)


def build(fake_gmail, fake_repo, fake_llm, settings, raw_messages, result=None):
    """Wire a Bridge over fakes, with one history page listing the messages."""
    page = {
        "history": [
            {"messagesAdded": [{"message": {"id": m["id"]}} for m in raw_messages]}
        ]
    }
    gmail = fake_gmail(
        history_pages=[page], messages={m["id"]: m for m in raw_messages}
    )
    repo = fake_repo()
    llm = fake_llm({TaskClassification: result or classification()})
    bridge = Bridge(
        gmail=gmail, github_repo=repo, llm=llm, settings=settings, known_projects=[REPO]
    )
    db.set_last_history_id(1, settings)
    return bridge, gmail, repo, llm


# -- webhook ---------------------------------------------------------------


@pytest.fixture
def pipeline_client(pipeline_settings):
    from fastapi.testclient import TestClient

    from pipeline import app as app_module

    # lifespan would init the DB and (if flagged) call Google; the settings
    # fixture already created the schema, so skip it entirely.
    return TestClient(app_module.app), app_module


def test_health_returns_ok(pipeline_client):
    client, _ = pipeline_client
    assert client.get("/health").json() == {"status": "ok"}


def test_webhook_rejects_missing_authorization(pipeline_client):
    client, _ = pipeline_client
    assert client.post("/gmail/webhook", json=envelope()).status_code == 401


def test_webhook_rejects_non_bearer_authorization(pipeline_client):
    client, _ = pipeline_client
    resp = client.post(
        "/gmail/webhook", json=envelope(), headers={"Authorization": "Basic abc"}
    )
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "error", [ValueError("bad signature"), ValueError("Wrong audience")]
)
def test_webhook_rejects_invalid_jwt(pipeline_client, monkeypatch, error):
    client, app_module = pipeline_client

    def boom(*a, **kw):
        raise error

    # Patch the importer's binding, not google's module — see tests/test_api.py.
    monkeypatch.setattr("pipeline.app.id_token.verify_oauth2_token", boom)
    resp = client.post(
        "/gmail/webhook", json=envelope(), headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 401


def test_webhook_rejects_unverified_email_claim(pipeline_client, monkeypatch):
    client, _ = pipeline_client
    monkeypatch.setattr(
        "pipeline.app.id_token.verify_oauth2_token",
        lambda *a, **kw: {"email_verified": False, "email": "x@y.com"},
    )
    resp = client.post(
        "/gmail/webhook", json=envelope(), headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 401


def test_webhook_valid_jwt_invokes_bridge(pipeline_client, monkeypatch):
    client, _ = pipeline_client
    monkeypatch.setattr(
        "pipeline.app.id_token.verify_oauth2_token",
        lambda *a, **kw: {"email_verified": True, "email": "pubsub@x.com"},
    )
    seen = []
    monkeypatch.setattr("pipeline.app.run_bridge", lambda hid: seen.append(hid))

    resp = client.post(
        "/gmail/webhook",
        json=envelope(history_id=777),
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 204
    assert seen == [777]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"message": {}},
        {"message": {"data": "!!!not-base64!!!"}},
        {"message": {"data": base64.urlsafe_b64encode(b"not json").decode()}},
    ],
)
def test_webhook_malformed_envelope_is_400(pipeline_client, monkeypatch, bad):
    client, _ = pipeline_client
    monkeypatch.setattr(
        "pipeline.app.id_token.verify_oauth2_token",
        lambda *a, **kw: {"email_verified": True, "email": "pubsub@x.com"},
    )
    resp = client.post("/gmail/webhook", json=bad, headers={"Authorization": "Bearer x"})
    # 400, not 500: Pub/Sub retries 5xx forever and this will never succeed.
    assert resp.status_code == 400


def test_decode_pubsub_envelope():
    from pipeline.app import decode_pubsub_envelope

    assert decode_pubsub_envelope(envelope("a@b.com", 12345)) == ("a@b.com", 12345)


# -- gates -----------------------------------------------------------------


def test_allowlist_reject_creates_no_issue(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    msg = make_raw_message(sender="stranger@evil.com")
    bridge, _, repo, llm = build(
        fake_gmail, fake_repo, fake_llm, pipeline_settings, [msg]
    )
    assert bridge.handle_notification(50) == []
    assert repo.created == []
    # Gate ordering is cheap-first: a rejected sender must not cost a token.
    assert llm.calls == []


def test_missing_task_prefix_creates_no_issue(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    msg = make_raw_message(subject="Add a health check")
    bridge, _, repo, llm = build(
        fake_gmail, fake_repo, fake_llm, pipeline_settings, [msg]
    )
    assert bridge.handle_notification(50) == []
    assert repo.created == []
    assert llm.calls == []


def test_reply_to_a_task_is_rejected(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    # Replies are discussion, not new work — otherwise every answer on a thread
    # opens another issue.
    msg = make_raw_message(subject="Re: [TASK] Add a health check")
    bridge, _, repo, _ = build(fake_gmail, fake_repo, fake_llm, pipeline_settings, [msg])
    assert bridge.handle_notification(50) == []
    assert repo.created == []


def test_non_actionable_creates_no_issue(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    bridge, _, repo, _ = build(
        fake_gmail,
        fake_repo,
        fake_llm,
        pipeline_settings,
        [make_raw_message()],
        result=classification(actionable=False),
    )
    assert bridge.handle_notification(50) == []
    assert repo.created == []


def test_unknown_project_creates_no_issue(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    bridge, _, repo, _ = build(
        fake_gmail,
        fake_repo,
        fake_llm,
        pipeline_settings,
        [make_raw_message()],
        result=classification(project="unknown"),
    )
    assert bridge.handle_notification(50) == []
    assert repo.created == []


# -- happy path and idempotency -------------------------------------------


def test_happy_path_creates_one_labeled_issue(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    bridge, _, repo, _ = build(
        fake_gmail, fake_repo, fake_llm, pipeline_settings, [make_raw_message()]
    )
    created = bridge.handle_notification(50)

    assert len(repo.created) == 1
    issue = repo.created[0]
    assert created == [issue.number]
    assert issue.title == "Add a health check"
    assert issue.labels == ["auto-task"]
    # The footer is the durable idempotency key; without it stale-claim
    # recovery cannot find the issue it already opened.
    assert "<!-- gmail-message-id: m1 -->" in issue.body
    assert db.get_last_history_id(pipeline_settings) == 50


def test_duplicate_delivery_creates_only_one_issue(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    msg = make_raw_message()
    page = {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}
    repo = fake_repo()
    llm = fake_llm({TaskClassification: classification()})
    db.set_last_history_id(1, pipeline_settings)

    for _ in range(2):
        gmail = fake_gmail(history_pages=[page], messages={"m1": msg})
        bridge = Bridge(
            gmail=gmail,
            github_repo=repo,
            llm=llm,
            settings=pipeline_settings,
            known_projects=[REPO],
        )
        bridge.handle_notification(50)

    assert len(repo.created) == 1


def test_stale_claim_adopts_existing_issue(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    """A crash between claim and creation must not open a second issue."""
    from tests.conftest import FakeIssue

    msg = make_raw_message()
    # Simulate the crash: the row exists, issue_number is still NULL.
    assert db.claim_message("m1", pipeline_settings) is True
    assert db.get_claim("m1", pipeline_settings)["issue_number"] is None

    existing = FakeIssue(99, "Add a health check", "body\n\n<!-- gmail-message-id: m1 -->", ["auto-task"])
    page = {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}
    gmail = fake_gmail(history_pages=[page], messages={"m1": msg})
    repo = fake_repo(existing_issues=[existing])
    bridge = Bridge(
        gmail=gmail,
        github_repo=repo,
        llm=fake_llm({TaskClassification: classification()}),
        settings=pipeline_settings,
        known_projects=[REPO],
    )
    db.set_last_history_id(1, pipeline_settings)

    assert bridge.handle_notification(50) == []
    assert repo.created == []
    assert db.get_claim("m1", pipeline_settings)["issue_number"] == 99


def _event_stages(settings, message_id=None):
    """Read the events table for assertions on what the bridge recorded."""
    with db.connect(settings) as conn:
        if message_id is None:
            rows = conn.execute("SELECT stage FROM events").fetchall()
        else:
            rows = conn.execute(
                "SELECT stage FROM events WHERE message_id = ?", (message_id,)
            ).fetchall()
    return [r["stage"] for r in rows]


def test_failing_message_is_dropped_and_cursor_advances(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    # A message that cannot be turned into an issue (here GitHub is down) must be
    # dropped and logged, NOT allowed to wedge the batch. Advancing the cursor is
    # what stops Pub/Sub from replaying the same poisoned message forever.
    msg = make_raw_message()
    page = {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}
    gmail = fake_gmail(history_pages=[page], messages={"m1": msg})
    repo = fake_repo(raise_on_create=RuntimeError("github is down"))
    bridge = Bridge(
        gmail=gmail,
        github_repo=repo,
        llm=fake_llm({TaskClassification: classification()}),
        settings=pipeline_settings,
        known_projects=[REPO],
    )
    db.set_last_history_id(1, pipeline_settings)

    # No raise: the failure is isolated.
    assert bridge.handle_notification(50) == []
    assert repo.created == []
    # Cursor advances so the batch does not replay.
    assert db.get_last_history_id(pipeline_settings) == 50
    # The drop is auditable.
    assert "process_error" in _event_stages(pipeline_settings, "m1")


def test_one_bad_message_does_not_block_siblings(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    # m1 fails classification (LLM raises), m2 succeeds. m2 must still become an
    # issue, m1 is dropped, and the cursor advances past both.
    m1 = make_raw_message(mid="m1")
    m2 = make_raw_message(mid="m2", subject="[TASK] Second thing")
    page = {
        "history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
            {"messagesAdded": [{"message": {"id": "m2"}}]},
        ]
    }
    gmail = fake_gmail(history_pages=[page], messages={"m1": m1, "m2": m2})
    repo = fake_repo()

    # FakeLLM raises for the first invoke, succeeds for the second.
    class FlakyLLM:
        def __init__(self):
            self.calls = 0

        def with_structured_output(self, schema, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("insufficient credits")
            return classification()

    bridge = Bridge(
        gmail=gmail,
        github_repo=repo,
        llm=FlakyLLM(),
        settings=pipeline_settings,
        known_projects=[REPO],
    )
    db.set_last_history_id(1, pipeline_settings)

    created = bridge.handle_notification(50)

    assert len(repo.created) == 1
    assert created == [repo.created[0].number]
    assert db.get_last_history_id(pipeline_settings) == 50
    assert "process_error" in _event_stages(pipeline_settings, "m1")
    assert "process_error" not in _event_stages(pipeline_settings, "m2")


# -- history handling ------------------------------------------------------


def test_cold_start_seeds_cursor_without_processing(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    msg = make_raw_message()
    page = {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}
    gmail = fake_gmail(history_pages=[page], messages={"m1": msg})
    repo = fake_repo()
    bridge = Bridge(
        gmail=gmail,
        github_repo=repo,
        llm=fake_llm({TaskClassification: classification()}),
        settings=pipeline_settings,
        known_projects=[REPO],
    )
    # No cursor stored: must seed, not backfill the whole mailbox.
    assert bridge.handle_notification(50) == []
    assert repo.created == []
    assert db.get_last_history_id(pipeline_settings) == 50


def test_history_pagination_collects_every_page(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    a = make_raw_message(mid="m1")
    b = make_raw_message(mid="m2", subject="[TASK] Second thing")
    pages = [
        {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]},
        {"history": [{"messagesAdded": [{"message": {"id": "m2"}}]}]},
    ]
    gmail = fake_gmail(history_pages=pages, messages={"m1": a, "m2": b})
    repo = fake_repo()
    bridge = Bridge(
        gmail=gmail,
        github_repo=repo,
        llm=fake_llm({TaskClassification: classification()}),
        settings=pipeline_settings,
        known_projects=[REPO],
    )
    db.set_last_history_id(1, pipeline_settings)

    assert len(bridge.handle_notification(50)) == 2
    assert [c for c in gmail.calls if c[0] == "messages.get"] == [
        ("messages.get", "m1"),
        ("messages.get", "m2"),
    ]


def test_duplicate_ids_within_one_page_fetched_once(
    fake_gmail, fake_repo, fake_llm, pipeline_settings, make_raw_message
):
    msg = make_raw_message()
    page = {
        "history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
            {"messagesAdded": [{"message": {"id": "m1"}}]},
        ]
    }
    gmail = fake_gmail(history_pages=[page], messages={"m1": msg})
    bridge = Bridge(
        gmail=gmail,
        github_repo=fake_repo(),
        llm=fake_llm({TaskClassification: classification()}),
        settings=pipeline_settings,
        known_projects=[REPO],
    )
    db.set_last_history_id(1, pipeline_settings)
    bridge.handle_notification(50)

    assert len([c for c in gmail.calls if c[0] == "messages.get"]) == 1


def test_expired_history_cursor_skips_ahead(
    fake_gmail, fake_repo, fake_llm, pipeline_settings
):
    """Gmail ages history out after ~a week; the cursor must not wedge."""
    from googleapiclient.errors import HttpError

    class Resp:
        status = 404
        reason = "Not Found"

    gmail = fake_gmail(
        history_error=HttpError(Resp(), b"expired"), profile_history_id=5000
    )
    repo = fake_repo()
    bridge = Bridge(
        gmail=gmail,
        github_repo=repo,
        llm=fake_llm({TaskClassification: classification()}),
        settings=pipeline_settings,
        known_projects=[REPO],
    )
    db.set_last_history_id(1, pipeline_settings)

    assert bridge.handle_notification(50) == []
    assert repo.created == []
    # Skipped ahead deliberately; the gap is NOT backfilled.
    assert db.get_last_history_id(pipeline_settings) == 50


# -- watch registration resilience -----------------------------------------


class _FakeScheduler:
    """Records add_job/remove_job; mirrors the hand-rolled-double style."""

    def __init__(self, running=False):
        self.running = running
        self.started = False
        self.jobs = {}          # id -> func
        self.removed = []

    def start(self):
        self.started = True
        self.running = True

    def add_job(self, func, trigger=None, **kwargs):
        self.jobs[kwargs.get("id")] = func

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self.jobs.pop(job_id, None)


def test_register_watch_boot_failure_is_non_fatal(monkeypatch, pipeline_settings):
    from pipeline import watch

    def boom(**kwargs):
        raise RuntimeError("gmail 429")

    monkeypatch.setattr(watch, "start_watch", boom)
    scheduler = _FakeScheduler()

    # Must not raise even though registration fails.
    watch.register_watch(scheduler=scheduler, settings=pipeline_settings)

    # A background retry job was scheduled.
    assert watch.BOOT_RETRY_JOB_ID in scheduler.jobs
    assert scheduler.running


def test_register_watch_retry_cancels_on_success(monkeypatch, pipeline_settings):
    from pipeline import watch

    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gmail 429")  # boot attempt fails
        # second attempt (the retry) succeeds

    monkeypatch.setattr(watch, "start_watch", flaky)
    scheduler = _FakeScheduler()

    watch.register_watch(scheduler=scheduler, settings=pipeline_settings)
    retry = scheduler.jobs[watch.BOOT_RETRY_JOB_ID]

    # Fire the retry: it succeeds and cancels itself.
    retry()
    assert watch.BOOT_RETRY_JOB_ID in scheduler.removed
    assert calls["n"] == 2


def test_register_watch_success_schedules_no_retry(monkeypatch, pipeline_settings):
    from pipeline import watch

    monkeypatch.setattr(watch, "start_watch", lambda **kwargs: None)
    scheduler = _FakeScheduler()

    watch.register_watch(scheduler=scheduler, settings=pipeline_settings)

    # Clean boot: no boot-retry job.
    assert watch.BOOT_RETRY_JOB_ID not in scheduler.jobs
