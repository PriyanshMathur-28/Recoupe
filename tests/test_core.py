"""Regression tests for revenue-risk detection and decisions."""
from modules.decision_engine import decide
from modules.detector import check_calendar_live, check_failed_subscriptions, normalize_event


class FakeRequest:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    def execute(self):
        if self.error:
            raise self.error
        return self.response


class FakeEvents:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.page_tokens = []

    def list(self, **kwargs):
        self.page_tokens.append(kwargs.get("pageToken"))
        response = next(self.responses)
        return FakeRequest(response=response) if isinstance(response, dict) else FakeRequest(error=response)


class FakeService:
    def __init__(self, responses):
        self.resource = FakeEvents(responses)

    def events(self):
        return self.resource


def test_decide_rejects_malformed_urgency_without_crashing():
    assert decide({"event_type": "no_show", "urgency_hours": "bad"}) == "escalate_human"
    assert decide({"event_type": "no_show", "urgency_hours": float("nan")}) == "escalate_human"


def test_decide_rejects_missing_or_malformed_attempt_count():
    assert decide({"event_type": "failed_subscription"}) == "escalate_human"
    assert decide({"event_type": "failed_subscription", "attempt_count": "bad"}) == "escalate_human"


def test_calendar_normalization_extracts_timestamps_and_urgency():
    event = normalize_event("calendar", {"id": "cal-1", "summary": "Visit", "status": "cancelled", "start": {"dateTime": "2026-09-01T10:00:00+05:30"}, "updated": "2026-09-01T08:30:00+05:30", "is_first_offense": "false"})
    assert event["appointment_datetime"] == "2026-09-01T10:00:00+05:30"
    assert event["urgency_hours"] == 1.5
    assert event["is_first_offense"] is False


def test_calendar_reader_paginates_and_filters_cancelled_events():
    service = FakeService([
        {"items": [{"id": "one", "status": "cancelled"}, {"id": "active", "status": "confirmed"}], "nextPageToken": "next"},
        {"items": [{"id": "two", "status": "cancelled"}]},
    ])
    events = check_calendar_live(service)
    assert [event["client_id"] for event in events] == ["one", "two"]
    assert service.resource.page_tokens == [None, "next"]


def test_calendar_reader_handles_api_failure():
    assert check_calendar_live(FakeService([RuntimeError("offline")])) == []


def test_normalize_event_rejects_pandas_missing_identifiers():
    import pandas as pd

    event = normalize_event("no_show", {"client_id": pd.NA})
    assert event["client_id"] is None
    assert "missing client_id" in event["validation_errors"]


def test_calendar_reader_preserves_valid_events_when_one_is_malformed():
    service = FakeService([{"items": [
        {"id": "good", "status": "cancelled", "start": {"dateTime": "2026-09-01T10:00:00+00:00"}, "updated": "2026-09-01T09:00:00+00:00"},
        {"id": "bad", "status": "cancelled", "start": object(), "updated": "2026-09-01T09:00:00+00:00"},
    ]}])
    events = check_calendar_live(service)
    assert len(events) == 2
    assert events[0]["client_id"] == "good"
    assert events[1]["event_type"] == "source_error"


def test_subscription_rows_are_valid_after_fixture_repair():
    events = check_failed_subscriptions()
    assert len(events) == 25
    assert all(not event["validation_errors"] for event in events)
    by_id = {event["client_id"]: event for event in events}
    assert by_id["SUB019"]["subscription_amount"] == 849
    assert by_id["SUB020"]["client_email"] == "anika.sen@example.com"


def test_subscription_pandas_missing_email_is_validation_error():
    import pandas as pd

    event = normalize_event("subscription", {"client_id": "S1", "attempt_count": 0, "subscription_amount": 10, "client_email": pd.NA})
    assert event["client_email"] is None
    assert "missing or invalid client_email" in event["validation_errors"]
