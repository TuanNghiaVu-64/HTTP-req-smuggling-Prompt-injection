"""
Automated test suite for the Indirect Prompt Injection Lab.

Tests Flask routes and input validation. Ollama HTTP requests are mocked
so that tests do not require the model to be running.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from app import app, comments, seed_comments


@pytest.fixture(autouse=True)
def reset_state():
    """Reset in-memory state before each test."""
    seed_comments()
    yield
    seed_comments()


@pytest.fixture
def client():
    """Create a Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# -----------------------------------------------------------------------
# Page routes
# -----------------------------------------------------------------------

class TestPageRoutes:
    def test_index_loads(self, client):
        rv = client.get("/")
        assert rv.status_code == 200
        assert b"User Comments" in rv.data

    def test_comments_page_loads(self, client):
        rv = client.get("/comments")
        assert rv.status_code == 200
        assert b"User Comments" in rv.data

    def test_comments_page_shows_seeded_comments(self, client):
        rv = client.get("/comments")
        assert b"Alice" in rv.data
        assert b"Bob" in rv.data
        assert b"TestUser" in rv.data
        assert b"Adversarial Test Data" in rv.data

    def test_chat_page_loads(self, client):
        rv = client.get("/chat")
        assert rv.status_code == 200
        assert b"AI Chat" in rv.data


# -----------------------------------------------------------------------
# Comment submission
# -----------------------------------------------------------------------

class TestCommentSubmission:
    def test_submit_valid_comment(self, client):
        rv = client.post("/api/comments", data={"author": "Tester", "text": "Nice product!"})
        assert rv.status_code == 200
        assert b"Comment added!" in rv.data
        assert b"Tester" in rv.data
        assert len(comments) == 4

    def test_get_api_comments_renders_page(self, client):
        rv = client.get("/api/comments")
        assert rv.status_code == 200
        assert b"User Comments" in rv.data

    def test_post_comments_page_directly(self, client):
        rv = client.post("/comments", data={"author": "DirectPoster", "text": "Direct comment"})
        assert rv.status_code == 200
        assert b"Comment added!" in rv.data
        assert b"DirectPoster" in rv.data

    def test_submit_comment_without_author(self, client):
        rv = client.post("/api/comments", data={"author": "", "text": "Anonymous comment"})
        assert rv.status_code == 200
        assert b"Anonymous" in rv.data

    def test_reject_empty_comment(self, client):
        rv = client.post("/api/comments", data={"author": "Tester", "text": ""})
        assert rv.status_code == 200
        assert b"Comment text cannot be empty" in rv.data
        assert len(comments) == 3

    def test_reject_whitespace_only_comment(self, client):
        rv = client.post("/api/comments", data={"author": "Tester", "text": "   "})
        assert rv.status_code == 200
        assert b"Comment text cannot be empty" in rv.data

    def test_reject_too_long_comment(self, client):
        rv = client.post("/api/comments", data={"author": "Tester", "text": "x" * 2001})
        assert rv.status_code == 200
        assert b"too long" in rv.data
        assert len(comments) == 3

    def test_reject_too_long_author(self, client):
        rv = client.post("/api/comments", data={"author": "A" * 101, "text": "ok"})
        assert rv.status_code == 200
        assert b"Author name is too long" in rv.data

    def test_html_in_comment_is_escaped(self, client):
        rv = client.post(
            "/api/comments",
            data={"author": "Hacker", "text": "<script>alert('xss')</script>"},
        )
        assert rv.status_code == 200
        assert b"<script>" not in rv.data
        assert b"&lt;script&gt;" in rv.data


# -----------------------------------------------------------------------
# Chat API
# -----------------------------------------------------------------------

def _mock_ollama_response(content="Test response from model"):
    """Create a mock Ollama successful response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {"role": "assistant", "content": content}
    }
    return mock_resp


class TestChatAPI:
    @patch("app.http_requests.post")
    def test_chat_vulnerable_mode(self, mock_post, client):
        mock_post.return_value = _mock_ollama_response("Here is a summary.")
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "Summarize comments", "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["response"] == "Here is a summary."
        assert data["mode"] == "vulnerable"
        assert "prompt_sent" in data

    @patch("app.http_requests.post")
    def test_chat_protected_mode(self, mock_post, client):
        mock_post.return_value = _mock_ollama_response("Protected answer.")
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "Summarize comments", "mode": "protected"}),
            content_type="application/json",
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["response"] == "Protected answer."
        assert data["mode"] == "protected"

        # Verify the protected prompt contains defensive instructions
        prompt = data["prompt_sent"]
        system_msg = prompt[0]["content"]
        assert "UNTRUSTED" in system_msg
        assert "NEVER follow" in system_msg

    @patch("app.http_requests.post")
    def test_chat_returns_prompt_for_inspection(self, mock_post, client):
        mock_post.return_value = _mock_ollama_response()
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "Test?", "mode": "vulnerable"}),
            content_type="application/json",
        )
        data = rv.get_json()
        prompt = data["prompt_sent"]
        assert len(prompt) == 2
        assert prompt[0]["role"] == "system"
        assert prompt[1]["role"] == "user"
        assert prompt[1]["content"] == "Test?"

    def test_chat_rejects_empty_question(self, client):
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "", "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 400
        assert "enter a question" in rv.get_json()["error"]

    def test_chat_rejects_too_long_question(self, client):
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "x" * 1001, "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 400
        assert "too long" in rv.get_json()["error"]

    def test_chat_rejects_invalid_json(self, client):
        rv = client.post(
            "/api/chat",
            data="not json",
            content_type="application/json",
        )
        assert rv.status_code == 400

    @patch("app.http_requests.post")
    def test_chat_handles_ollama_connection_error(self, mock_post, client):
        import requests
        mock_post.side_effect = requests.ConnectionError("refused")
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "Hello", "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 502
        assert "Cannot connect to Ollama" in rv.get_json()["error"]

    @patch("app.http_requests.post")
    def test_chat_handles_ollama_timeout(self, mock_post, client):
        import requests
        mock_post.side_effect = requests.Timeout("timed out")
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "Hello", "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 504
        assert "timed out" in rv.get_json()["error"]

    @patch("app.http_requests.post")
    def test_chat_handles_model_not_found(self, mock_post, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"error": "model 'xyz' not found"}
        mock_resp.text = "model 'xyz' not found"
        mock_post.return_value = mock_resp
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "Hello", "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 404
        assert "not found" in rv.get_json()["error"]

    @patch("app.http_requests.post")
    def test_chat_handles_malformed_ollama_response(self, mock_post, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"unexpected": "format"}
        mock_post.return_value = mock_resp
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "Hello", "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 502
        assert "unexpected response" in rv.get_json()["error"].lower()

    @patch("app.http_requests.post")
    def test_chat_handles_general_request_exception(self, mock_post, client):
        import requests
        mock_post.side_effect = requests.RequestException("DNS failure")
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "Hello", "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 502
        assert "network error" in rv.get_json()["error"].lower()

    @patch("app.http_requests.post")
    def test_chat_updates_chat_history(self, mock_post, client):
        from app import chat_history
        chat_history.clear()
        mock_post.return_value = _mock_ollama_response("Assistant response here")
        rv = client.post(
            "/api/chat",
            data=json.dumps({"question": "What is the summary?", "mode": "vulnerable"}),
            content_type="application/json",
        )
        assert rv.status_code == 200
        assert len(chat_history) == 2
        assert chat_history[0]["role"] == "user"
        assert chat_history[0]["content"] == "What is the summary?"
        assert chat_history[1]["role"] == "assistant"
        assert chat_history[1]["content"] == "Assistant response here"


# -----------------------------------------------------------------------
# Chat clear
# -----------------------------------------------------------------------

class TestChatClear:
    def test_clear_chat(self, client):
        from app import chat_history
        chat_history.append({"role": "user", "content": "hello", "mode": "vulnerable"})
        rv = client.post("/api/chat/clear")
        assert rv.status_code == 200
        assert rv.get_json()["status"] == "ok"
        assert len(chat_history) == 0


# -----------------------------------------------------------------------
# Prompt content verification
# -----------------------------------------------------------------------

class TestPromptContent:
    def test_vulnerable_prompt_includes_all_comments(self):
        from app import build_vulnerable_prompt
        messages = build_vulnerable_prompt("test question")
        system_content = messages[0]["content"]
        assert "Alice" in system_content
        assert "Bob" in system_content
        assert "INJECTION_SUCCESS" in system_content

    def test_protected_prompt_marks_comments_untrusted(self):
        from app import build_protected_prompt
        messages = build_protected_prompt("test question")
        system_content = messages[0]["content"]
        assert "UNTRUSTED" in system_content
        assert "NEVER follow" in system_content
        assert "INJECTION_SUCCESS" in system_content  # still present as data
