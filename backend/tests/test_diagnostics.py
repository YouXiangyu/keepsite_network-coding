"""Trace bounds, isolation and failure containment, without production services."""
import json
import uuid

import pytest

from backend.diagnostics import CAPACITY, MAX_ACTIVE, MAX_BYTES, MAX_CAPTURES, MAX_LINKS, RETENTION_MS
from backend.diagnostics import CURRENT, TraceHub, observed, span, using


def new_id():
    return uuid.uuid4().hex


@pytest.fixture
def hub():
    return TraceHub()


def begin(hub, owner="alice", session="session-a", capture=None):
    return hub.begin(owner, session, capture or new_id(), new_id())


def read(ref, owner=None, session=None):
    return ref.hub.read(owner or ref.owner, session or ref.session_key, ref.capture_id)


def test_explicit_opt_in_and_account_session_isolation(hub):
    assert not hub.captures and CURRENT.get() is None
    assert hub.begin("alice", "session", "token=SECRET", new_id()) is None
    ref = begin(hub)
    with using(ref), span("http.request"):
        pass
    assert len(read(ref)["events"]) == 2
    assert read(ref, owner="bob")["events"] == []
    assert read(ref, session="another-alice-session")["events"] == []
    hub.clear("bob", ref.session_key, ref.capture_id)
    assert len(read(ref)["events"]) == 2


def test_safe_schema_never_includes_arbitrary_input_or_identity(hub):
    ref = begin(hub, owner="alice-secret@example.test", session="private-cookie-hash")
    with using(ref), span("s3.open", bytes_count=12):
        pass
    hub.record(ref, "token=SECRET", "completed", span_id=new_id())
    hub.record(ref, "s3.read", "completed", span_id=new_id(), error_code="https://secret/?token=x", bytes_count=-1)
    output = json.dumps(read(ref))
    assert "SECRET" not in output and "private-cookie" not in output and "example.test" not in output
    assert "https://" not in output
    assert read(ref)["events"][-1].get("errorCode") is None
    assert read(ref)["events"][-1].get("bytes") is None


def test_nested_spans_have_same_trace_and_real_parent(hub):
    ref = begin(hub)
    with using(ref), span("http.request") as parent:
        with span("sql.commit"):
            pass
    events = read(ref)["events"]
    child = [event for event in events if event["event"] == "sql.commit"]
    assert all(event["traceId"] == ref.trace_id and event["parentSpanId"] == parent for event in child)
    assert all(event["durationMs"] >= 0 for event in events)
    assert CURRENT.get() is None


def test_failure_records_failure_and_propagates_original_exception(hub):
    ref = begin(hub)
    error = ValueError("password=do-not-record")
    with pytest.raises(ValueError) as result:
        with using(ref), span("worker.ocr"):
            raise error
    assert result.value is error
    assert read(ref)["events"][-1]["status"] == "failed"
    assert "do-not-record" not in json.dumps(read(ref))


def test_observer_failures_do_not_change_success_or_error(hub, monkeypatch):
    ref = begin(hub)
    def broken(*args, **kwargs):
        raise RuntimeError("diagnostics failed")
    monkeypatch.setattr(hub, "record", broken)
    @observed("http.request")
    def work():
        return 42
    with using(ref):
        assert work() == 42
        with pytest.raises(LookupError):
            with span("worker.ocr"):
                raise LookupError("business")
    monkeypatch.setattr(hub, "monotonic", broken)
    with using(ref):
        assert work() == 42


def test_clear_and_revoke_invalidate_late_refs_and_inflight_admission(hub):
    ref = begin(hub)
    hub.remember("job", "private-job-id", ref)
    hub.clear(ref.owner, ref.session_key, ref.capture_id)
    assert hub.begin(ref.owner, ref.session_key, ref.capture_id, new_id()) is None
    with using(ref), span("worker.ocr"):
        pass
    assert read(ref)["events"] == []
    assert hub.recall("job", ref.owner, "private-job-id") is None
    second = begin(hub)
    hub.revoke(second.session_key)
    with using(second), span("http.request"):
        pass
    assert read(second)["events"] == []


def test_ttl_is_absolute_and_old_ref_cannot_write_new_generation():
    tick = [100.0]
    hub = TraceHub(monotonic=lambda: tick[0], wall=lambda: 1_000)
    ref = begin(hub)
    tick[0] += RETENTION_MS
    assert read(ref)["expiresAt"] is None
    fresh = hub.begin(ref.owner, ref.session_key, ref.capture_id, new_id())
    assert fresh.generation != ref.generation
    with using(ref), span("http.request"):
        pass
    assert read(fresh)["events"] == []


def test_event_memory_active_slots_and_context_links_are_bounded(hub):
    ref = begin(hub)
    for _ in range(CAPACITY + 60):
        hub.record(ref, "s3.read", "completed", span_id=new_id(), bytes_count=100)
    result = read(ref)
    assert len(result["events"]) == CAPACITY and result["dropped"] == 60
    assert hub.captures[(ref.owner, ref.session_key, ref.capture_id)].size <= MAX_BYTES
    for _ in range(MAX_ACTIVE):
        assert hub.slot(ref, 1)
    assert not hub.slot(ref, 1)
    for index in range(MAX_LINKS + 20):
        hub.remember("job", str(index), ref)
    assert len(hub.links) == MAX_LINKS
    for index in range(MAX_CAPTURES + 20):
        begin(hub, owner=str(index))
    assert len(hub.captures) == MAX_CAPTURES


def test_incremental_reads_have_stable_sequences_and_are_copies(hub):
    ref = begin(hub)
    with using(ref), span("http.request"):
        pass
    first = read(ref)
    first["events"][0]["event"] = "tampered"
    assert read(ref)["events"][0]["event"] == "http.request"
    assert hub.read(ref.owner, ref.session_key, ref.capture_id, after=1)["events"][0]["sequence"] == 2


def test_reserved_span_fields_cannot_replace_business_exception_or_result(hub):
    ref, error = begin(hub), ValueError("business")
    with using(ref):
        with span("http.request", duration_ms=7, span_id="bad", started=-1, status="bad"):
            result = 42
        assert result == 42
        with pytest.raises(ValueError) as caught:
            with span("worker.ocr", error_code="timeout", duration_ms=3, span_id="bad"):
                raise error
        assert caught.value is error
    assert CURRENT.get() is None
    capture = hub.captures[(ref.owner, ref.session_key, ref.capture_id)]
    assert capture.active == 0
    assert read(ref)["events"][-1]["errorCode"] == "timeout"


def test_all_captures_share_one_monotonic_origin_but_own_ttl():
    tick = [10.0]
    hub = TraceHub(monotonic=lambda: tick[0], wall=lambda: 1_000)
    first = begin(hub)
    tick[0] = 20.0
    second = begin(hub)
    tick[0] = 30.0
    for ref in (first, second):
        with using(ref), span("http.request"):
            pass
    assert read(first)["clockId"] == read(second)["clockId"]
    assert read(first)["events"][0]["offsetMs"] == read(second)["events"][0]["offsetMs"] == 20.0
    tick[0] = 10 + RETENTION_MS
    assert read(first)["expiresAt"] is None and read(second)["expiresAt"] is not None


def test_link_keys_are_typed_and_bounded_without_arbitrary_string_conversion(hub):
    ref = begin(hub)
    class Secret:
        def __str__(self):
            raise AssertionError("must not stringify")
    for kind, key in (("job", "x" * 129), ("job", Secret()), ("cursor", "123"), ("cursor", True)):
        hub.remember(kind, key, ref)
        assert hub.recall(kind, ref.owner, key) is None
    assert not hub.links
    hub.remember("job", "some-job", ref)
    hub.remember("cursor", 2, ref)
    assert hub.recall("job", ref.owner, "some-job") is ref
    assert hub.recall("cursor", ref.owner, 2) is ref


def test_retired_capture_quota_cannot_starve_other_accounts(hub):
    from backend.diagnostics import MAX_PER_ACCOUNT
    retired = []
    for _ in range(MAX_PER_ACCOUNT):
        ref = begin(hub)
        retired.append(ref)
        hub.clear(ref.owner, ref.session_key, ref.capture_id)
    assert begin(hub) is None
    assert len(hub.captures) == MAX_PER_ACCOUNT
    assert begin(hub, owner="bob", session="b") is not None
    for ref in retired:
        assert hub.begin(ref.owner, ref.session_key, ref.capture_id, new_id()) is None


def test_finished_span_has_event_time_and_explicit_start_in_one_clock_domain():
    tick = [0.0]
    hub = TraceHub(monotonic=lambda: tick[0], wall=lambda: 1_000)
    ref = begin(hub)
    tick[0] = 10.0
    with using(ref), span("worker.prepare"):
        tick[0] = 30.0
    started, finished = read(ref)["events"]
    assert started["spanId"] == finished["spanId"]
    assert finished["offsetMs"] == 30 and finished["startOffsetMs"] == 10 and finished["durationMs"] == 20


def test_outcome_can_cancel_without_changing_business_return_or_exception(hub):
    ref = begin(hub)
    with using(ref), span("worker.publish", outcome=lambda: "cancelled"):
        result = False
    assert result is False and read(ref)["events"][-1]["status"] == "cancelled"
    with using(ref), span("worker.publish", outcome=lambda: (_ for _ in ()).throw(RuntimeError())):
        result = 7
    assert result == 7 and CURRENT.get() is None
