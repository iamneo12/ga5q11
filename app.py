"""
GA5 Observable Incident Agent - FastAPI implementation.

Routes:
  POST /v2/incidents
  POST /v2/incidents/{runId}/receipts
  GET  /v2/incidents/{runId}

Run locally:
  export GEMINI_API_KEY=...
  uvicorn app:app --host 0.0.0.0 --port 8000

Deploy anywhere that gives you a public HTTPS URL (Fly.io recommended for an
always-on free instance so you don't eat cold-start latency against the
18s per-request budget).
"""
import json
import traceback
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

import storage
import model
from otlp import TraceBuilder
from util import request_hash, sorted_json_digest, new_hex_id, new_trace_id

app = FastAPI()


# TEMPORARY DEBUG HANDLER - remove before grading. Surfaces the real
# traceback in the HTTP response instead of a bare "Internal Server Error",
# so you can see failures directly in curl output.
@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "error": str(exc),
            "type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def current_view(state: dict) -> dict:
    """Builds the response body reflecting the run's current pending work,
    or the final result if the run is terminal."""
    if state["status"] in ("completed", "failed"):
        return {
            "runId": state["run_id"],
            "status": state["status"],
            "diagnosis": {"rootCause": state["plan"]["rootCause"], "evidence": state["plan"]["evidence"]},
            "chosenEffect": state.get("chosen_effect"),
            "suppressed": state.get("suppressed", []),
            "actionLog": state["action_log"],
            "receiptLog": state["receipt_log"],
            "otlp": TraceBuilder.from_stored(state["trace"]).to_otlp(),
        }

    dispatches = []
    for a in state["pending_dispatches"]:
        dispatches.append(a)

    approvals = []
    if state.get("pending_approval"):
        pa = state["pending_approval"]
        approvals.append({
            "approvalId": pa["approvalId"],
            "actionId": pa["actionId"],
            "toolName": pa["toolName"],
            "argumentsDigest": pa["argumentsDigest"],
        })

    resp = {
        "runId": state["run_id"],
        "status": "waiting",
        "diagnosis": {"rootCause": state["plan"]["rootCause"], "evidence": state["plan"]["evidence"]},
    }
    if dispatches:
        resp["dispatches"] = dispatches
    if approvals:
        resp["approvals"] = approvals
    if not dispatches and not approvals:
        # nothing pending but not yet terminal - shouldn't normally happen
        resp["dispatches"] = []
        resp["approvals"] = []
    return resp


def all_diagnostics_resolved(state: dict) -> bool:
    for a in state["diagnostic_actions"].values():
        if a["status"] not in ("confirmed", "failed"):
            return False
    return True


def any_diagnostic_failed(state: dict) -> bool:
    return any(a["status"] == "failed" for a in state["diagnostic_actions"].values())


def maybe_advance_to_effect(state: dict, tb: TraceBuilder):
    """Called once all diagnostics are resolved. Either queues the effect
    dispatch, requests approval, or marks the run failed/suppressed."""
    if state["effect_stage"] != "pending_diagnostics":
        return

    if any_diagnostic_failed(state):
        state["effect_stage"] = "suppressed"
        state["suppressed"].append({
            "toolName": state["plan"]["effect"]["toolName"],
            "reason": "diagnostic_failed_or_timed_out",
        })
        state["status"] = "failed"
        return

    effect = state["plan"]["effect"]
    tool_name = effect["toolName"]
    policy = state["policy"]
    action_id = state["effect_action_id"]

    if tool_name in policy.get("approvalRequiredFor", []):
        approval_id = new_hex_id()
        digest = sorted_json_digest(effect["arguments"])
        state["pending_approval"] = {
            "approvalId": approval_id,
            "actionId": action_id,
            "toolName": tool_name,
            "argumentsDigest": digest,
        }
        tb.add_approval_span(approval_id)
        state["effect_stage"] = "awaiting_approval"
    else:
        dispatch_effect(state, tb)


def dispatch_effect(state: dict, tb: TraceBuilder):
    effect = state["plan"]["effect"]
    action_id = state["effect_action_id"]
    call_id = action_id
    tb.add_execute_tool_span(action_id, effect["toolName"])
    call_span_id = tb.add_tool_call_span(action_id, call_id, effect["toolName"], attempt=1)
    dispatch = {
        "actionId": action_id,
        "callId": call_id,
        "phase": "effect",
        "toolName": effect["toolName"],
        "arguments": effect["arguments"],
        "attempt": 1,
        "traceparent": f"00-{tb.trace_id}-{call_span_id}-01",
    }
    if state.get("pending_approval") and state["pending_approval"]["actionId"] == action_id:
        dispatch["approvalId"] = state["pending_approval"]["approvalId"]
    state["action_log"].append(dispatch)
    state["pending_dispatches"] = [dispatch]
    state["effect_actions"][action_id] = {
        "callId": call_id, "toolName": effect["toolName"], "attempt": 1, "status": "pending",
    }
    state["effect_stage"] = "dispatched"


# ---------------------------------------------------------------------------
# POST /v2/incidents
# ---------------------------------------------------------------------------

@app.post("/v2/incidents")
async def create_incident(request: Request):
    body = await request.json()

    for key in ("profile", "runId", "agentName", "incident", "toolCatalog", "policy"):
        if key not in body:
            raise HTTPException(status_code=422, detail=f"missing field: {key}")
    if body["profile"] != "ga5-incident-agent/v2":
        raise HTTPException(status_code=400, detail="unsupported profile")

    run_id = body["runId"]
    req_hash = request_hash(body)

    existing = storage.get_run(run_id)
    if existing is not None:
        if existing["request_hash"] != req_hash:
            raise HTTPException(status_code=409, detail="runId exists with different content")
        return JSONResponse(current_view(existing["state"]))

    incident = body["incident"]
    tool_catalog = body["toolCatalog"]
    policy = body["policy"]
    public_marker = body.get("publicMarker", "")
    agent_name = body.get("agentName", "incident-response")

    incoming_traceparent = body.get("incident", {}).get("traceparent") or request.headers.get("traceparent")
    trace_id = None
    parent_span_id = None
    if incoming_traceparent:
        parts = incoming_traceparent.split("-")
        if len(parts) == 4 and len(parts[1]) == 32:
            trace_id = parts[1]
            parent_span_id = parts[2]
    if trace_id is None:
        trace_id = new_trace_id()

    tb = TraceBuilder(trace_id, run_id, public_marker, agent_name)
    tb.add_server_span(incoming_traceparent_span_id=parent_span_id)
    tb.add_agent_span()
    tb.add_plan_span(model.MODEL_NAME)

    plan = model.plan_incident(incident, tool_catalog, policy)

    diagnostic_actions = {}
    action_log = []
    diag_action_ids = []
    for diag in plan["diagnostics"]:
        action_id = new_hex_id()
        call_id = action_id
        diag_action_ids.append(action_id)
        tb.add_execute_tool_span(action_id, diag["toolName"])
        call_span_id = tb.add_tool_call_span(action_id, call_id, diag["toolName"], attempt=1)
        dispatch = {
            "actionId": action_id,
            "callId": call_id,
            "phase": "diagnostic",
            "toolName": diag["toolName"],
            "arguments": diag["arguments"],
            "evidence": diag["evidence"],
            "attempt": 1,
            "traceparent": f"00-{trace_id}-{call_span_id}-01",
        }
        action_log.append(dispatch)
        diagnostic_actions[action_id] = {
            "callId": call_id, "toolName": diag["toolName"], "attempt": 1, "status": "pending",
        }

    if len(diag_action_ids) > 1:
        tb.add_join_span(diag_action_ids)

    state = {
        "run_id": run_id,
        "policy": policy,
        "plan": plan,
        "diagnostic_actions": diagnostic_actions,
        "effect_action_id": new_hex_id(),
        "effect_actions": {},
        "effect_stage": "pending_diagnostics",
        "pending_approval": None,
        "pending_dispatches": action_log.copy(),
        "action_log": action_log,
        "receipt_log": [],
        "suppressed": [],
        "chosen_effect": None,
        "status": "waiting",
        "trace": tb.to_stored(),
    }

    storage.save_run(run_id, req_hash, state)
    return JSONResponse(current_view(state))


# ---------------------------------------------------------------------------
# POST /v2/incidents/{runId}/receipts
# ---------------------------------------------------------------------------

@app.post("/v2/incidents/{run_id}/receipts")
async def post_receipts(run_id: str, request: Request):
    body = await request.json()
    existing = storage.get_run(run_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="unknown runId")
    state = existing["state"]

    receipt_id = body.get("receiptId")
    req_hash = request_hash(body)

    prior_receipt = storage.get_receipt(receipt_id) if receipt_id else None
    if prior_receipt is not None:
        if prior_receipt["request_hash"] != req_hash:
            raise HTTPException(status_code=409, detail="receiptId exists with different content")
        return JSONResponse(prior_receipt["result"])

    tb = TraceBuilder.from_stored(state["trace"])
    state["pending_dispatches"] = []

    # --- process outcomes ---
    for outcome in body.get("outcomes", []):
        action_id = outcome["actionId"]
        call_id = outcome["callId"]
        attempt = outcome["attempt"]
        http_status = outcome.get("status")
        result_class = outcome.get("resultClass")
        error_type = outcome.get("errorType")
        nonce = outcome.get("nonce")

        state["receipt_log"].append({
            "receiptId": receipt_id, "actionId": action_id, "callId": call_id,
            "attempt": attempt, "status": http_status, "resultClass": result_class,
            "nonce": nonce,
        })

        bucket = state["diagnostic_actions"] if action_id in state["diagnostic_actions"] else state["effect_actions"]
        record = bucket.get(action_id)
        if record is None or record["callId"] != call_id or record["attempt"] != attempt:
            # only accept outcomes for pending calls we actually issued
            continue

        if http_status == 200:
            record["status"] = "confirmed"
            record["resultClass"] = result_class
            tb.add_tool_call_span(action_id, call_id, record["toolName"], attempt,
                                   http_status=200, receipt_id=receipt_id, receipt_nonce=nonce)
        elif http_status == 503 and record.get("retried") != True:
            record["retried"] = True
            new_call_id = new_hex_id()
            new_attempt = attempt + 1
            tb.add_tool_call_span(action_id, call_id, record["toolName"], attempt,
                                   http_status=503, receipt_id=receipt_id, receipt_nonce=nonce)
            retry_span_id = tb.add_tool_call_span(action_id, new_call_id, record["toolName"], new_attempt)
            record["callId"] = new_call_id
            record["attempt"] = new_attempt
            record["status"] = "pending"
            retry_dispatch = {
                "actionId": action_id, "callId": new_call_id, "phase": "diagnostic" if action_id in state["diagnostic_actions"] else "effect",
                "toolName": record["toolName"], "attempt": new_attempt,
                "traceparent": f"00-{tb.trace_id}-{retry_span_id}-{new_attempt:02d}",
            }
            state["action_log"].append(retry_dispatch)
            state["pending_dispatches"].append(retry_dispatch)
        elif error_type == "timeout" or http_status == 0:
            record["status"] = "failed"
            tb.add_tool_call_span(action_id, call_id, record["toolName"], attempt,
                                   error_type="timeout", receipt_id=receipt_id, receipt_nonce=nonce)
        else:
            record["status"] = "failed"
            tb.add_tool_call_span(action_id, call_id, record["toolName"], attempt,
                                   http_status=http_status, receipt_id=receipt_id, receipt_nonce=nonce)

    # --- process approval decisions ---
    for approval in body.get("approvals", []):
        pa = state.get("pending_approval")
        if pa is None or approval.get("approvalId") != pa["approvalId"]:
            continue
        state["receipt_log"].append({
            "receiptId": receipt_id, "approvalId": approval["approvalId"],
            "decision": approval.get("decision"), "nonce": approval.get("nonce"),
        })
        if approval.get("decision") == "approved":
            dispatch_effect(state, tb)
        else:
            state["effect_stage"] = "suppressed"
            state["suppressed"].append({"toolName": pa["toolName"], "reason": "approval_denied"})
            state["status"] = "failed"
        state["pending_approval"] = None

    # --- advance state machine ---
    if state["effect_stage"] == "pending_diagnostics" and all_diagnostics_resolved(state):
        maybe_advance_to_effect(state, tb)

    if state["effect_actions"]:
        eff_id, eff = next(iter(state["effect_actions"].items()))
        if eff["status"] == "confirmed":
            state["chosen_effect"] = eff["toolName"]
            state["status"] = "completed"
        elif eff["status"] == "failed":
            state["status"] = "failed"

    state["trace"] = tb.to_stored()
    result = current_view(state)

    storage.save_run(run_id, existing["request_hash"], state)
    if receipt_id:
        storage.save_receipt(receipt_id, run_id, req_hash, result)

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# GET /v2/incidents/{runId}
# ---------------------------------------------------------------------------

@app.get("/v2/incidents/{run_id}")
async def get_incident(run_id: str):
    existing = storage.get_run(run_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="unknown runId")
    return JSONResponse(current_view(existing["state"]))