"""family_brain: the cloud-or-local decision, costs and budgets (no real API calls)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace as NS

import pytest

import family_brain as fb
from hub_client import HubClient


@pytest.fixture
def ollama():
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            out = json.dumps({"message": {"content": "<think>hmm</think>Local answer."}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", calls
    srv.shutdown()


def usage(i=1000, o=500, web=0):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0,
              server_tool_use=NS(web_search_requests=web))


class FakeClaude:
    """Looks enough like anthropic.Anthropic for family_brain."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = NS(create=self._create)
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def reply(text="Cloud answer.", stop="end_turn", model="claude-sonnet-5-5", web=0, extra=()):
    return NS(model=model, stop_reason=stop, usage=usage(web=web), stop_details=None,
              content=list(extra) + [NS(type="text", text=text)])


@pytest.fixture
def scholar(server, tokens, tmp_path):
    return HubClient(tokens["scholar"], server, outbox=tmp_path / "o.jsonl")


def brain(hub, ollama_url, client=None, online=True):
    return fb.FamilyBrain(hub, system="You are Scholar.", ollama_url=ollama_url,
                          cloud_client=client, internet_check=lambda: online)


def test_local_only_channel_never_uses_cloud(scholar, ollama, hub):
    hub.set_budget("scholar", 5)
    claude = FakeClaude([reply()])
    t = brain(scholar, ollama[0], claude).think("secret study question", local_only=True)
    assert t.brain == "local" and t.text == "Local answer." and claude.calls == []
    assert ollama[1][0]["messages"][0] == {"role": "system", "content": "You are Scholar."}


def test_no_budget_uses_local_and_research_waits(scholar, ollama):
    claude = FakeClaude([reply()])
    b = brain(scholar, ollama[0], claude)
    t = b.think("hi", job="chat")
    assert t.brain == "local" and "no cloud budget" in t.reason
    w = b.think("research this", job="research")
    assert w.brain == "wait" and claude.calls == []


def test_cloud_call_reports_cost_to_hub(scholar, ollama, hub):
    hub.set_budget("scholar", 5)
    found = NS(type="web_search_tool_result", content=[NS(url="https://example.org/a"), NS(url="https://example.org/b")])
    claude = FakeClaude([reply(web=2, extra=[found])])
    t = brain(scholar, ollama[0], claude).think("find studio backup prices", job="research")
    assert t.brain == "cloud" and t.text == "Cloud answer." and t.sources == ["https://example.org/a",
                                                                              "https://example.org/b"]
    call = claude.calls[0]
    assert call["model"] == "claude-sonnet-5-5" and call["fallbacks"] == "default"
    assert call["tools"][0]["type"] == "web_search_20260209" and call["output_config"] == {"effort": "medium"}
    # 1000 in * $2 + 500 out * $10 per million, + 2 searches
    assert t.cost_usd == pytest.approx(0.002 + 0.005 + 0.02)
    assert hub.budget_status(hub.member("scholar"))["spent"] == pytest.approx(t.cost_usd)


def test_chat_uses_haiku_without_fallbacks_or_web(scholar, ollama, hub):
    hub.set_budget("scholar", 5)
    claude = FakeClaude([reply(model="claude-haiku-5-5")])
    t = brain(scholar, ollama[0], claude).think("hello", job="chat")
    call = claude.calls[0]
    assert t.model == "claude-haiku-5-5" and "fallbacks" not in call and "tools" not in call
    assert call["output_config"] == {"effort": "low"}


def test_pause_turn_is_continued(scholar, ollama, hub):
    hub.set_budget("scholar", 5)
    claude = FakeClaude([reply(stop="pause_turn", text=""), reply(text="Done researching.")])
    t = brain(scholar, ollama[0], claude).think("deep research", job="research")
    assert t.text == "Done researching." and len(claude.calls) == 2
    assert claude.calls[1]["messages"][-1]["role"] == "assistant"


def test_refusal_is_not_retried_locally(scholar, ollama, hub):
    hub.set_budget("scholar", 5)
    claude = FakeClaude([NS(model="claude-sonnet-5-5", stop_reason="refusal", usage=usage(o=0), content=[],
                            stop_details=NS(explanation="declined"))])
    t = brain(scholar, ollama[0], claude).think("something", job="code")
    assert t.refused and t.brain == "cloud" and ollama[1] == []


def test_offline_or_over_budget_falls_back(scholar, ollama, hub):
    hub.set_budget("scholar", 5)
    assert brain(scholar, ollama[0], FakeClaude([]), online=False).think("hi").brain == "local"
    hub.set_budget("scholar", 0.001)
    hub.report_usage(hub.member("scholar"), "claude-haiku-5-5", 0, 0, 0.001)
    t = brain(scholar, ollama[0], FakeClaude([])).think("hi")
    assert t.brain == "local" and "budget" in t.reason


def test_cloud_outage_falls_back_to_local(scholar, ollama, hub):
    anthropic = pytest.importorskip("anthropic")
    import httpx2 as httpx

    hub.set_budget("scholar", 5)

    class Down(FakeClaude):
        def _create(self, **kw):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    t = brain(scholar, ollama[0], Down([])).think("hi", job="chat")
    assert t.brain == "local" and "unavailable" in t.reason


def test_no_ollama_and_no_cloud_is_a_clear_error(scholar):
    with pytest.raises(fb.BrainUnavailable) as e:
        brain(scholar, "http://127.0.0.1:9").think("hi")
    assert "Ollama" in str(e.value)


def test_price_table():
    assert fb.price("claude-opus-5-5", 1_000_000, 0) == 4.0
    assert fb.price("claude-haiku-5-5", 200_000, 0) == pytest.approx(0.10)  # long-prompt rate $0.50/M
