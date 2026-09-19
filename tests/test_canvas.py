from datetime import datetime, timezone

import httpx
import pytest
import respx

from instinct.adapters.canvas import (CanvasAuthError, CanvasClient, CanvasError, TokenTransport, html_to_text,
                                      next_link)

BASE = "https://canvas.test.edu"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
COURSES = [{"id": 11, "name": "Systems Programming", "course_code": "CSC 357"},
           {"id": 22, "name": "Linear Analysis", "course_code": "MATH 244"}]


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    return CanvasClient(BASE, TokenTransport("tok", httpx.Client()), sleep=sleeps.append, now=lambda: NOW)


def link(url: str, rel: str = "next") -> dict:
    return {"Link": f'<{url}>; rel="current",<{url}>; rel="{rel}",<{BASE}/x?page=first>; rel="first"'}


def test_next_link_parsing():
    h = f'<{BASE}/a?page=1>; rel="current",<{BASE}/a?page=2>; rel="next",<{BASE}/a?page=9>; rel="last"'
    assert next_link(h) == f"{BASE}/a?page=2"
    assert next_link(f'<{BASE}/a?page=9>; rel="last"') is None
    assert next_link(None) is None


@respx.mock
def test_pagination_follows_link_header_and_sends_token(client):
    p2 = f"{BASE}/api/v1/courses?page=bookmark:abc&per_page=100"
    route = respx.get(f"{BASE}/api/v1/courses").mock(side_effect=[
        httpx.Response(200, json=[{"id": 1}], headers=link(p2)),
        httpx.Response(200, json=[{"id": 2}], headers={"Link": f'<{BASE}/x>; rel="first"'}),
    ])
    assert client.get_all("/api/v1/courses", [("enrollment_state", "active")]) == [{"id": 1}, {"id": 2}]
    first, second = route.calls
    assert first.request.headers["Authorization"] == "Bearer tok"
    assert first.request.url.params["per_page"] == "100"
    assert first.request.url.params["enrollment_state"] == "active"
    assert str(second.request.url) == p2  # next link used verbatim, no duplicated params


@respx.mock
def test_rate_limit_429_and_legacy_403_are_retried(client, sleeps):
    respx.get(f"{BASE}/api/v1/users/self/todo").mock(side_effect=[
        httpx.Response(429, text="429 Rate Limit Exceeded"),
        httpx.Response(403, text="403 Forbidden (Rate Limit Exceeded)"),
        httpx.Response(503),
        httpx.Response(200, json=[]),
    ])
    assert client.get_all("/api/v1/users/self/todo") == []
    assert sleeps == [1, 2, 4]


@respx.mock
def test_retry_after_and_low_quota_slowdown(client, sleeps):
    respx.get(f"{BASE}/api/v1/users/self/todo").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json=[], headers={"X-Rate-Limit-Remaining": "12.5"}),
    ])
    client.get_all("/api/v1/users/self/todo")
    assert sleeps == [7.0, 1.0]


@respx.mock
def test_gives_up_after_max_retries(sleeps):
    c = CanvasClient(BASE, TokenTransport("t", httpx.Client()), max_retries=2, sleep=sleeps.append)
    respx.get(f"{BASE}/api/v1/users/self/todo").mock(return_value=httpx.Response(429))
    with pytest.raises(CanvasError, match="throttling"):
        c.todo()
    assert len(sleeps) == 2


@respx.mock
def test_plain_403_and_401_are_not_retried(client, sleeps):
    respx.get(f"{BASE}/api/v1/users/self/todo").mock(return_value=httpx.Response(401))
    with pytest.raises(CanvasAuthError):
        client.todo()
    respx.get(f"{BASE}/api/v1/courses/11/assignments").mock(return_value=httpx.Response(403, text="unauthorized"))
    respx.get(f"{BASE}/api/v1/courses").mock(return_value=httpx.Response(200, json=COURSES))
    with pytest.raises(CanvasError, match="403"):
        client.course_assignments("CSC 357")
    assert sleeps == []


@respx.mock
def test_upcoming_uses_planner_window(client):
    respx.get(f"{BASE}/api/v1/courses").mock(return_value=httpx.Response(200, json=COURSES))
    route = respx.get(f"{BASE}/api/v1/planner/items").mock(return_value=httpx.Response(200, json=[
        {"plannable_type": "assignment", "course_id": 22, "plannable_date": "2026-09-20T06:59:00Z",
         "plannable": {"title": "HW 2", "points_possible": 10}, "html_url": "/courses/22/assignments/5",
         "submissions": {"submitted": False, "missing": False, "graded": False}},
        {"plannable_type": "quiz", "context_name": "Systems Programming", "plannable_date": "2026-09-19T17:00:00Z",
         "plannable": {"title": "Quiz 1"}, "submissions": False, "planner_override": {"marked_complete": True}},
    ]))
    items = client.upcoming(days=3)
    params = route.calls[0].request.url.params
    assert params["start_date"] == "2026-09-18T12:00:00+00:00"
    assert params["end_date"] == "2026-09-21T12:00:00+00:00"
    assert [i["title"] for i in items] == ["Quiz 1", "HW 2"]  # sorted by due date
    assert items[1]["course"] == "MATH 244"
    assert items[1]["url"] == f"{BASE}/courses/22/assignments/5"
    assert items[0]["completed"] is True


@respx.mock
def test_course_assignments_resolves_course_by_code_or_name(client):
    respx.get(f"{BASE}/api/v1/courses").mock(return_value=httpx.Response(200, json=COURSES))
    route = respx.get(f"{BASE}/api/v1/courses/11/assignments").mock(return_value=httpx.Response(200, json=[
        {"id": 1, "name": "Lab 1", "due_at": "2026-09-25T06:59:00Z", "description": "<p>Write <b>ls</b></p>",
         "submission": {"workflow_state": "unsubmitted", "missing": False}},
    ]))
    for ref in ("csc357", "CSC 357", "systems", 11):
        out = client.course_assignments(ref)
        assert out["course"]["id"] == 11
    assert out["assignments"][0]["description"] == "Write ls"
    assert route.calls[0].request.url.params["bucket"] == "future"
    with pytest.raises(CanvasError, match="no active course"):
        client.course_assignments("art history")


@respx.mock
def test_announcements_uses_context_codes(client):
    respx.get(f"{BASE}/api/v1/courses").mock(return_value=httpx.Response(200, json=COURSES))
    route = respx.get(f"{BASE}/api/v1/announcements").mock(return_value=httpx.Response(200, json=[
        {"id": 5, "title": "Midterm moved", "context_code": "course_22", "posted_at": "2026-09-17T10:00:00Z",
         "message": "<p>Now on <strong>Friday</strong>.</p><script>x()</script>", "author": {"display_name": "Prof"}},
        {"id": 4, "title": "old", "context_code": "course_11", "posted_at": "2026-09-01T10:00:00Z", "message": ""},
    ]))
    out = client.announcements(since="2026-09-10")
    params = route.calls[0].request.url.params
    assert params.get_list("context_codes[]") == ["course_11", "course_22"]
    assert params["start_date"] == "2026-09-10"
    assert [a["title"] for a in out] == ["Midterm moved"]
    assert out[0]["course"] == "MATH 244" and out[0]["message"] == "Now on Friday."


def test_html_to_text():
    assert html_to_text("<p>a&amp;b</p><p>c</p>") == "a&b\n\nc"
    assert html_to_text(None) == ""
    assert html_to_text("x" * 10, limit=5) == "xxxxx…"


def test_requires_base_url():
    with pytest.raises(CanvasError):
        CanvasClient("", TokenTransport("t", httpx.Client()))
