"""
Real provider adapters, matching each SDK's actual current interface.
These CANNOT be exercised in this sandbox -- no network, no SDK install
possible here. They're written to be correct and ready, not simulated.

Requires (once you have network + keys):
  pip install anthropic openai
  export ANTHROPIC_API_KEY=...
  export OPENAI_API_KEY=...
  export XAI_API_KEY=...
"""
import os, time, json
from kernel import ModelAdapter, ModelRequest, ModelResponse

# Shared across all three adapters so the relation-trust policy can't drift
# between them. ABOUT is applied immediately (project() only ever treats it
# as optional fill). DEPENDS_ON/CONTRADICTS are the structural hinges
# project() treats as must-keep, so a model's own unverified self-report on
# one of those is logged for human review instead of written directly.
EVIDENCE_CLASS_INSTRUCTIONS = (
    "On evidence_class: a deterministic, step-by-step trace or deduction "
    "through an already-known rule or mechanism (e.g. manually working "
    "through code logic to its fixed conclusion) is INFERRED, not SIMULATED. "
    "Reserve SIMULATED specifically for modeling a stochastic or "
    "underspecified process forward, where the outcome is not fixed by pure "
    "logical necessity from what is already known."
)

RELATION_FIELD_INSTRUCTIONS = (
    'Optionally add a "relation" field if this candidate meaningfully '
    'connects to an existing claim already visible in PROJECTED WORKSPACE: '
    '{"target_claim_id": "<id from the workspace>", "relation_type": '
    '"ABOUT|DEPENDS_ON|CONTRADICTS", "rationale": "..."}. ABOUT relations '
    "are applied automatically -- they're low-stakes decoration. "
    "DEPENDS_ON and CONTRADICTS are structural hinges that gate what "
    "future work treats as load-bearing, so a proposal of either of "
    "those is logged for human review, not applied automatically -- your "
    "own say-so about a structural dependency or contradiction doesn't "
    "get the same automatic trust as everything else here. Only set "
    "target_claim_id to an id actually shown to you in PROJECTED "
    "WORKSPACE, never one you're inferring or recalling."
)


class ClaudeAdapter(ModelAdapter):
    name = "claude"

    def __init__(self, model="claude-sonnet-5", api_key=None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model

    def ping(self) -> str:
        """Raw connectivity/billing check -- bypasses the PRCSA operator
        system prompt entirely. generate()'s system prompt forbids plain-text
        replies (it forces reasoning + a JSON candidate or HOLD), so a smoke
        test routed through generate() can never get a literal 'PONG' back
        no matter how healthy the connection is."""
        resp = self.client.messages.create(
            model=self.model, max_tokens=10,
            messages=[{"role": "user", "content": "Reply with exactly the word PONG and nothing else."}],
        )
        return next((b.text for b in resp.content if b.type == "text"), "")

    def generate(self, request: ModelRequest) -> ModelResponse:
        start = time.time()
        system_prompt = (
            "You are an operator over a persistent relational substrate (PRCSA). "
            "You will receive a task and a projected workspace (a bounded slice of "
            "current state). Respond with plain reasoning, then end with a single "
            "JSON block on its own line describing ONE candidate structural "
            "contribution, in exactly this shape:\n"
            '{"content": "...", "scope": {...}, "evidence_class": "REAL|SIMULATED|'
            'PREDICTED|INTERVENTION_DERIVED|INFERRED", "kind": "CLAIM|HYPOTHESIS|'
            'PREDICTION|QUESTION|SIMULATION|ACTION_PROPOSAL|INTERPRETATION|'
            'UNCERTAINTY|TRANSLATION|HOLD", "falsification_test": null}\n'
            f"{EVIDENCE_CLASS_INSTRUCTIONS}\n"
            f"{RELATION_FIELD_INSTRUCTIONS}\n"
            "If you do not have enough support to commit to a claim, respond with "
            '{"kind": "HOLD", "content": "why"} instead -- this is a legitimate, '
            "preferred outcome, not a failure. Do not invent a CLAIM just to "
            "produce output. If evidence_class is INFERRED, falsification_test "
            "MUST be a concrete, checkable prediction, not null. You do not have "
            "write access to the substrate -- the gate decides what happens with "
            "this candidate."
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=request.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": (
                f"TASK: {request.task}\n\nPROJECTED WORKSPACE:\n"
                f"{json.dumps(request.projection, indent=2)}"
            )}],
        )
        latency = (time.time() - start) * 1000
        # Claude runs adaptive thinking by default, so content[0] may be a
        # ThinkingBlock rather than the TextBlock -- scan for the text block
        # instead of assuming position 0.
        raw_text = next((b.text for b in resp.content if b.type == "text"), "")
        candidate = _extract_json_candidate(raw_text)
        return ModelResponse(
            raw_text=raw_text, model_id=self.model, latency_ms=latency,
            token_usage={"input": resp.usage.input_tokens, "output": resp.usage.output_tokens},
            candidate=candidate,
        )


class GPTAdapter(ModelAdapter):
    name = "gpt"

    def __init__(self, model="gpt-5", api_key=None):
        import openai
        self.client = openai.OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        self.model = model

    def ping(self) -> str:
        """Raw connectivity/billing check -- bypasses the PRCSA operator
        system prompt (see ClaudeAdapter.ping for why that's necessary).
        gpt-5 is a reasoning model: its (invisible) reasoning tokens are
        billed against max_completion_tokens same as the visible reply, so
        a budget of 10 gets consumed entirely by reasoning and returns an
        empty completion with finish_reason=length. Found live -- 10
        reliably starved the reply to nothing; 256 leaves enough headroom
        for reasoning plus the one-word reply."""
        resp = self.client.chat.completions.create(
            model=self.model, max_completion_tokens=256,
            messages=[{"role": "user", "content": "Reply with exactly the word PONG and nothing else."}],
        )
        return resp.choices[0].message.content or ""

    def generate(self, request: ModelRequest) -> ModelResponse:
        start = time.time()
        system_prompt = (
            "You are an operator over a persistent relational substrate (PRCSA). "
            "Respond with reasoning, then a single JSON block: "
            '{"content": "...", "scope": {...}, "evidence_class": "REAL|SIMULATED|'
            'PREDICTED|INTERVENTION_DERIVED|INFERRED", "kind": "CLAIM|HYPOTHESIS|'
            'PREDICTION|QUESTION|SIMULATION|ACTION_PROPOSAL|INTERPRETATION|'
            'UNCERTAINTY|TRANSLATION|HOLD", "falsification_test": null}. '
            f"{EVIDENCE_CLASS_INSTRUCTIONS} "
            f"{RELATION_FIELD_INSTRUCTIONS} "
            "If you do not have enough support to commit to a claim, respond "
            'with {"kind": "HOLD", "content": "why"} instead -- this is a '
            "legitimate, preferred outcome, not a failure. Do not invent a "
            "CLAIM just to produce output. INFERRED requires a real "
            "falsification_test. You cannot write to the substrate directly "
            "-- the gate decides."
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=request.max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    f"TASK: {request.task}\n\nPROJECTED WORKSPACE:\n"
                    f"{json.dumps(request.projection, indent=2)}"
                )},
            ],
        )
        latency = (time.time() - start) * 1000
        raw_text = resp.choices[0].message.content
        candidate = _extract_json_candidate(raw_text)
        return ModelResponse(
            raw_text=raw_text, model_id=self.model, latency_ms=latency,
            token_usage={"input": resp.usage.prompt_tokens, "output": resp.usage.completion_tokens},
            candidate=candidate,
        )


class GrokAdapter(ModelAdapter):
    name = "grok"

    def __init__(self, model="grok-4", api_key=None):
        import openai  # xAI's API is OpenAI-compatible
        self.client = openai.OpenAI(
            api_key=api_key or os.environ.get("XAI_API_KEY"),
            base_url="https://api.x.ai/v1",
        )
        self.model = model

    def ping(self) -> str:
        """Raw connectivity/billing check -- bypasses the PRCSA operator
        system prompt (see ClaudeAdapter.ping for why that's necessary)."""
        resp = self.client.chat.completions.create(
            model=self.model, max_tokens=10,
            messages=[{"role": "user", "content": "Reply with exactly the word PONG and nothing else."}],
        )
        return resp.choices[0].message.content or ""

    def generate(self, request: ModelRequest) -> ModelResponse:
        start = time.time()
        system_prompt = (
            "You are an operator over a persistent relational substrate (PRCSA). "
            "Respond with reasoning, then a single JSON block: "
            '{"content": "...", "scope": {...}, "evidence_class": "REAL|SIMULATED|'
            'PREDICTED|INTERVENTION_DERIVED|INFERRED", "kind": "CLAIM|HYPOTHESIS|'
            'PREDICTION|QUESTION|SIMULATION|ACTION_PROPOSAL|INTERPRETATION|'
            'UNCERTAINTY|TRANSLATION|HOLD", "falsification_test": null}. '
            f"{EVIDENCE_CLASS_INSTRUCTIONS} "
            f"{RELATION_FIELD_INSTRUCTIONS} "
            "If you do not have enough support to commit to a claim, respond "
            'with {"kind": "HOLD", "content": "why"} instead -- this is a '
            "legitimate, preferred outcome, not a failure. Do not invent a "
            "CLAIM just to produce output. INFERRED requires a real "
            "falsification_test. The gate decides admission, not you."
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=request.max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    f"TASK: {request.task}\n\nPROJECTED WORKSPACE:\n"
                    f"{json.dumps(request.projection, indent=2)}"
                )},
            ],
        )
        latency = (time.time() - start) * 1000
        raw_text = resp.choices[0].message.content
        candidate = _extract_json_candidate(raw_text)
        return ModelResponse(
            raw_text=raw_text, model_id=self.model, latency_ms=latency,
            token_usage={"input": resp.usage.prompt_tokens, "output": resp.usage.completion_tokens},
            candidate=candidate,
        )


def _extract_json_candidate(raw_text: str):
    """Pulls the last JSON object out of the response text using a
    STRING-AWARE balanced-brace scanner. The earlier version tracked
    brace depth globally, which broke on a real, plausible case: a
    model's natural-language content containing an unmatched brace
    character (e.g. discussing code or notation). Found by testing
    the actual failure case, not assumed safe just because a similar
    example happened to work. This version tracks whether we're
    inside a JSON string literal and ignores braces while inside one,
    correctly handling escaped quotes too."""
    candidates = []
    depth = 0
    start_idx = None
    in_string = False
    escape_next = False
    for i, ch in enumerate(raw_text):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue  # braces inside a string are just characters, not structure
        if ch == '{':
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx is not None:
                    candidates.append(raw_text[start_idx:i+1])
                    start_idx = None
    for candidate_text in reversed(candidates):
        try:
            obj = json.loads(candidate_text)
        except json.JSONDecodeError:
            continue
        if obj.get("kind") == "HOLD" and "content" in obj:
            return obj
        if "content" in obj and "evidence_class" in obj:
            return obj
    return None

def smoke_test(adapter_class, **kwargs):
    """The literal first thing to run once keys exist: reply PONG,
    confirm the key/billing/connection works, before spending anything
    on a real PRCSA task. Uses adapter.ping() -- a raw call with no PRCSA
    operator system prompt -- since generate()'s system prompt forbids
    plain-text replies and would route this trivial request into HOLD
    regardless of whether the connection is healthy."""
    adapter = adapter_class(**kwargs)
    start = time.time()
    raw_text = adapter.ping()
    latency = (time.time() - start) * 1000
    print(f"[{adapter.name}] raw response: {raw_text!r}")
    print(f"[{adapter.name}] latency: {latency:.1f}ms")
    return raw_text.strip().upper() == "PONG"
      
