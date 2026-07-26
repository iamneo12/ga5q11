"""
Builds the OTLP trace (resourceSpans JSON) for a run, matching:

SERVER   POST /v2/incidents
  INTERNAL invoke_agent incident-response          (exactly one)
    CLIENT   chat incident-plan                    (one per logical action... here: exactly one)
    INTERNAL execute_tool <toolName>                (one per logical executed action)
      CLIENT POST tool/<toolName>                   (one per physical attempt)
    INTERNAL incident.join                          (when diagnostics fan out)
    INTERNAL approval_gate                           (when approval is required)

All spans share one trace ID. Span IDs are unique nonzero lowercase hex.
"""
import time
from util import new_span_id

KIND_INTERNAL = "SPAN_KIND_INTERNAL"
KIND_CLIENT = "SPAN_KIND_CLIENT"
KIND_SERVER = "SPAN_KIND_SERVER"

STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2


def _now_ns():
    return int(time.time() * 1e9)


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


class TraceBuilder:
    def __init__(self, trace_id: str, run_id: str, public_marker: str, agent_name: str):
        self.trace_id = trace_id
        self.run_id = run_id
        self.public_marker = public_marker
        self.agent_name = agent_name
        self.spans = []
        self.server_span_id = None
        self.agent_span_id = None
        self.plan_span_id = None
        self.join_span_id = None
        self.approval_span_id = None
        # actionId -> execute_tool span id (for join linking / approval linking)
        self.action_spans = {}

    def _base_attrs(self):
        return [_attr("ga5.run.id", self.run_id), _attr("ga5.public.marker", self.public_marker)]

    def add_server_span(self, incoming_traceparent_span_id=None):
        span_id = new_span_id()
        self.server_span_id = span_id
        self.spans.append({
            "traceId": self.trace_id,
            "spanId": span_id,
            "parentSpanId": incoming_traceparent_span_id or "",
            "name": "POST /v2/incidents",
            "kind": KIND_SERVER,
            "startTimeUnixNano": str(_now_ns()),
            "endTimeUnixNano": str(_now_ns()),
            "attributes": self._base_attrs(),
            "status": {"code": STATUS_UNSET},
        })
        return span_id

    def add_agent_span(self):
        span_id = new_span_id()
        self.agent_span_id = span_id
        self.spans.append({
            "traceId": self.trace_id,
            "spanId": span_id,
            "parentSpanId": self.server_span_id,
            "name": "invoke_agent incident-response",
            "kind": KIND_INTERNAL,
            "startTimeUnixNano": str(_now_ns()),
            "endTimeUnixNano": str(_now_ns()),
            "attributes": self._base_attrs() + [_attr("gen_ai.operation.name", "invoke_agent")],
            "status": {"code": STATUS_UNSET},
        })
        return span_id

    def add_plan_span(self, model_name: str):
        span_id = new_span_id()
        self.plan_span_id = span_id
        self.spans.append({
            "traceId": self.trace_id,
            "spanId": span_id,
            "parentSpanId": self.agent_span_id,
            "name": "chat incident-plan",
            "kind": KIND_CLIENT,
            "startTimeUnixNano": str(_now_ns()),
            "endTimeUnixNano": str(_now_ns()),
            "attributes": self._base_attrs() + [
                _attr("gen_ai.operation.name", "chat"),
                _attr("gen_ai.request.model", model_name),
            ],
            "status": {"code": STATUS_UNSET},
        })
        return span_id

    def add_execute_tool_span(self, action_id: str, tool_name: str):
        span_id = new_span_id()
        self.action_spans[action_id] = span_id
        self.spans.append({
            "traceId": self.trace_id,
            "spanId": span_id,
            "parentSpanId": self.agent_span_id,
            "name": f"execute_tool {tool_name}",
            "kind": KIND_INTERNAL,
            "startTimeUnixNano": str(_now_ns()),
            "endTimeUnixNano": str(_now_ns()),
            "attributes": self._base_attrs() + [
                _attr("gen_ai.operation.name", "execute_tool"),
                _attr("gen_ai.tool.name", tool_name),
                _attr("ga5.action.id", action_id),
            ],
            "status": {"code": STATUS_UNSET},
        })
        return span_id

    def add_tool_call_span(self, action_id, call_id, tool_name, attempt,
                            http_status=None, error_type=None, receipt_id=None,
                            receipt_nonce=None):
        parent = self.action_spans.get(action_id, self.agent_span_id)
        span_id = new_span_id()
        attrs = self._base_attrs() + [
            _attr("gen_ai.tool.call.id", call_id),
            _attr("ga5.action.id", action_id),
            _attr("ga5.attempt", attempt),
            _attr("http.request.method", "POST"),
            _attr("http.request.resend_count", max(attempt - 1, 0)),
        ]
        if receipt_id:
            attrs.append(_attr("ga5.receipt.id", receipt_id))
        if receipt_nonce:
            attrs.append(_attr("ga5.receipt.nonce", receipt_nonce))

        status = {"code": STATUS_UNSET}
        if error_type == "timeout":
            status = {"code": STATUS_ERROR, "message": "timeout"}
            attrs.append(_attr("error.type", "timeout"))
        elif http_status == 503:
            status = {"code": STATUS_ERROR}
        elif http_status == 200:
            status = {"code": STATUS_UNSET}

        self.spans.append({
            "traceId": self.trace_id,
            "spanId": span_id,
            "parentSpanId": parent,
            "name": f"POST tool/{tool_name}",
            "kind": KIND_CLIENT,
            "startTimeUnixNano": str(_now_ns()),
            "endTimeUnixNano": str(_now_ns()),
            "attributes": attrs,
            "status": status,
        })
        return span_id

    def add_join_span(self, diagnostic_action_ids):
        span_id = new_span_id()
        self.join_span_id = span_id
        self.spans.append({
            "traceId": self.trace_id,
            "spanId": span_id,
            "parentSpanId": self.agent_span_id,
            "name": "incident.join",
            "kind": KIND_INTERNAL,
            "startTimeUnixNano": str(_now_ns()),
            "endTimeUnixNano": str(_now_ns()),
            "attributes": self._base_attrs() + [
                _attr("ga5.action.id", ",".join(diagnostic_action_ids)),
            ],
            "status": {"code": STATUS_UNSET},
        })
        return span_id

    def add_approval_span(self, approval_id, receipt_id=None):
        span_id = new_span_id()
        self.approval_span_id = span_id
        attrs = self._base_attrs() + [_attr("ga5.approval.id", approval_id)]
        if receipt_id:
            attrs.append(_attr("ga5.receipt.id", receipt_id))
        self.spans.append({
            "traceId": self.trace_id,
            "spanId": span_id,
            "parentSpanId": self.agent_span_id,
            "name": "approval_gate",
            "kind": KIND_INTERNAL,
            "startTimeUnixNano": str(_now_ns()),
            "endTimeUnixNano": str(_now_ns()),
            "attributes": attrs,
            "status": {"code": STATUS_UNSET},
        })
        return span_id

    def to_otlp(self):
        return {
            "resourceSpans": [{
                "resource": {"attributes": [_attr("service.name", self.agent_name)]},
                "scopeSpans": [{
                    "scope": {"name": "ga5-incident-agent"},
                    "spans": self.spans,
                }],
            }]
        }

    @classmethod
    def from_stored(cls, data: dict):
        """Rehydrate a builder from previously stored span list (for replay -
        we never rebuild spans, only re-serve them, but this helper exists
        in case in-flight mutation is needed before finalization)."""
        tb = cls(data["trace_id"], data["run_id"], data["public_marker"], data["agent_name"])
        tb.spans = data["spans"]
        tb.server_span_id = data.get("server_span_id")
        tb.agent_span_id = data.get("agent_span_id")
        tb.plan_span_id = data.get("plan_span_id")
        tb.join_span_id = data.get("join_span_id")
        tb.approval_span_id = data.get("approval_span_id")
        tb.action_spans = data.get("action_spans", {})
        return tb

    def to_stored(self):
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "public_marker": self.public_marker,
            "agent_name": self.agent_name,
            "spans": self.spans,
            "server_span_id": self.server_span_id,
            "agent_span_id": self.agent_span_id,
            "plan_span_id": self.plan_span_id,
            "join_span_id": self.join_span_id,
            "approval_span_id": self.approval_span_id,
            "action_spans": self.action_spans,
        }
