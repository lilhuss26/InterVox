"""Gmail notification -> GitHub issue.

Collaborators (Gmail, the repo handle, the LLM, the storage module) are injected
through the constructor. build_bridge() is the only impure factory, so tests
construct a Bridge with fakes and patch nothing.
"""

import logging
import uuid

from googleapiclient.errors import HttpError

from pipeline import github_client
from pipeline.config import get_settings
from pipeline.gates import passes_allowlist, passes_subject_prefix
from pipeline.gmail.parser import parse_message
from pipeline.llm import classify_email
from pipeline.storage import db as db_module

log = logging.getLogger(__name__)


class Bridge:
    def __init__(
        self,
        gmail,
        github_repo,
        llm,
        settings=None,
        db=db_module,
        known_projects=None,
    ):
        self.gmail = gmail
        self.github_repo = github_repo
        self.llm = llm
        self.settings = settings or get_settings()
        self.db = db
        self.known_projects = known_projects or [self.settings.github_repo]

    # -- history ---------------------------------------------------------

    def _list_new_message_ids(self, start_history_id: int) -> list[str]:
        """Every messageAdded id since the cursor, first-seen order.

        Returns [] and resets the cursor if Gmail has aged the cursor out. A
        non-404 HttpError propagates: it is a batch-level, genuinely transient
        failure (rate limit, 5xx) with nothing to skip, so letting the webhook
        500 and Pub/Sub retry is the right behaviour. Per-message fetching is
        deliberately NOT done here — the caller fetches each id in isolation so
        one bad message cannot abort the whole batch.
        """
        try:
            request = (
                self.gmail.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                )
            )
            message_ids: list[str] = []
            seen: set[str] = set()
            while request is not None:
                page = request.execute()
                for record in page.get("history", []):
                    for added in record.get("messagesAdded", []):
                        mid = added.get("message", {}).get("id")
                        # One page can report the same message under several
                        # records; keep first-seen order but never duplicate.
                        if mid and mid not in seen:
                            seen.add(mid)
                            message_ids.append(mid)
                request = self.gmail.users().history().list_next(request, page)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                self._recover_expired_cursor()
                return []
            raise

        return message_ids

    def _fetch_message(self, message_id: str) -> dict:
        """Fully fetch one message by id."""
        return (
            self.gmail.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    def _recover_expired_cursor(self) -> None:
        """Gmail keeps history ~1 week; past that startHistoryId 404s.

        Jump the cursor to now rather than retrying forever. The gap is
        deliberately NOT backfilled — replaying a week of mail could open
        dozens of issues at once.
        """
        profile = self.gmail.users().getProfile(userId="me").execute()
        current = int(profile["historyId"])
        log.warning("history cursor expired; skipping ahead to %s", current)
        self.db.set_last_history_id(current, self.settings)
        self.db.log_event(str(uuid.uuid4()), "history_expired", settings=self.settings)

    # -- per message -----------------------------------------------------

    def process_message(self, raw: dict, correlation_id: str) -> int | None:
        parsed = parse_message(raw)
        message_id = parsed["message_id"]

        def drop(stage: str) -> None:
            self.db.log_event(correlation_id, stage, message_id, settings=self.settings)

        if not self.db.claim_message(message_id, self.settings):
            claim = self.db.get_claim(message_id, self.settings)
            if claim and claim["issue_number"] is not None:
                drop("skip_duplicate")
                return None
            # Claimed but no issue number: a previous run died mid-flight. The
            # issue may or may not exist, so ask GitHub rather than guess.
            existing = github_client.find_issue_by_message_id(
                self.github_repo, message_id
            )
            if existing is not None:
                self.db.mark_processed(message_id, existing, self.settings)
                drop("recovered_stale_claim")
                return None
            log.warning("stale claim for %s with no issue; recreating", message_id)

        if not passes_allowlist(parsed["sender"], self.settings.allowlist):
            drop("reject_allowlist")
            return None

        if not passes_subject_prefix(parsed["subject"]):
            drop("reject_subject")
            return None

        result = classify_email(self.llm, parsed, self.known_projects)
        if result.project not in self.known_projects or not result.actionable:
            drop("reject_llm")
            return None

        body = github_client.build_issue_body(
            result.description, result.acceptance, parsed["sender"], message_id
        )
        issue_number = github_client.create_issue(self.github_repo, result.title, body)

        # Only now is the message genuinely done.
        self.db.mark_processed(message_id, issue_number, self.settings)
        self.db.log_event(
            correlation_id, "issue_created", message_id, issue_number, self.settings
        )
        return issue_number

    # -- entry point -----------------------------------------------------

    def handle_notification(self, history_id: int, correlation_id: str | None = None):
        correlation_id = correlation_id or str(uuid.uuid4())
        start = self.db.get_last_history_id(self.settings)

        if start is None:
            # Cold start: seed the cursor and process nothing. Backfilling an
            # entire mailbox on first boot would be a very loud mistake.
            self.db.set_last_history_id(history_id, self.settings)
            self.db.log_event(correlation_id, "cold_start", settings=self.settings)
            return []

        created = []
        for mid in self._list_new_message_ids(start):
            # Isolate each message: a failure in fetch/classify/create (e.g. the
            # LLM when Anthropic credits run out) is logged and the message
            # dropped, rather than raised. Raising here would 500 the webhook and
            # — because the cursor only advanced on full success — make Pub/Sub
            # replay the same poisoned message forever.
            try:
                raw = self._fetch_message(mid)
                number = self.process_message(raw, correlation_id)
                if number is not None:
                    created.append(number)
            except Exception:
                log.exception("dropping message %s after processing failure", mid)
                self.db.log_event(
                    correlation_id, "process_error", mid, settings=self.settings
                )

        # Always advance: a message that cannot be processed is dropped (logged
        # as process_error above), never allowed to wedge the batch.
        self.db.set_last_history_id(history_id, self.settings)
        return created


def build_bridge(settings=None) -> Bridge:
    """The one impure factory: real Gmail, real GitHub, real Anthropic."""
    from pipeline.auth.gmail_auth import get_gmail_service
    from pipeline.llm import get_llm

    settings = settings or get_settings()
    settings.require("GITHUB_TOKEN", "GITHUB_REPO", "ANTHROPIC_API_KEY")
    return Bridge(
        gmail=get_gmail_service(settings),
        github_repo=github_client.get_repo(settings),
        llm=get_llm(),
        settings=settings,
    )


def run_bridge(history_id: int) -> None:
    """Module-level entry point for the webhook."""
    build_bridge().handle_notification(history_id)
