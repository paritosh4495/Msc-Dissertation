# Builds and writes one JSON artifact file per trial.
# Called by orchestrator.py immediately after graph.invoke() returns.
#
# One file per trial, named by trial_id:
#   trial_results/<experiment_id>/F1_condA_rep01_<timestamp>.json

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from harness.fault_catalogue import GroundTruth, get_ground_truth
from harness.harness_config import PRICE_PER_1K_INPUT_TOKENS, PRICE_PER_1K_OUTPUT_TOKENS
from harness.progress import TrialSpec
from harness.materialisation import MaterialisationLog

logger = logging.getLogger(__name__)

# Outcome categories — used in scoring

# EC  = Exact Correct        — all three fields match ground truth
# PC  = Partial Correct      — at least one but not all fields match
# WD  = Wrong Diagnosis      — submitted but all fields wrong
# NFD = No Fault Detected    — agent said no fault; fault was active
# NS  = No Submission        — agent never called submit_diagnosis
# SL  = Step Limit           — hit max steps without submitting

OUTCOME_EC = "EC"
OUTCOME_PC = "PC"
OUTCOME_WD = "WD"
OUTCOME_NFD = "NFD"
OUTCOME_NS = "NS"
OUTCOME_SL = "SL"

# Public entry point


def build_and_write(
    spec: TrialSpec,
    final_state: dict,
    output_dir: Path,
    trial_start_utc: str,
    trial_end_utc: str,
    materialisation_log: MaterialisationLog,
) -> Path:
    """
    Build the trial artifact from final_state and write it atomically to disk.

    Args:
        spec:                 TrialSpec — trial_id, fault_id, condition, repetition.
        final_state:          The dict returned by graph.invoke().
        output_dir:           Dated experiment output directory.
        trial_start_utc:      ISO 8601 — when graph.invoke() was called.
        trial_end_utc:        ISO 8601 — when graph.invoke() returned.
        materialisation_log:  Produced by environment.py during materialisation.

    Returns:
        Path to the written artifact file.
    """
    messages: list[BaseMessage] = final_state.get("messages", [])
    step_count: int = final_state.get("step_count", 0)
    terminated: bool = final_state.get("terminated", False)
    system_prompt: str = final_state.get("system_prompt", "")

    # Extract each section independently.
    # Each function is self-contained and handles its own edge cases.
    submission = _extract_submission(messages)
    tool_trace = _extract_tool_trace(messages)
    token_usage = _extract_token_usage(messages)
    termination = _determine_termination(terminated, messages)
    ground_truth = get_ground_truth(spec.fault_id)
    scoring = _score(submission, ground_truth, termination)
    session_summary = _build_session_summary(messages, step_count, termination,
                                             system_prompt, tool_trace)

    # trial_id in the filename includes a timestamp so files from different
    # runs of the same trial_id (e.g. after manual re-run of a corrupted trial)
    # do not silently overwrite each other.
    timestamp_tag = trial_start_utc.replace(":",
                                            "").replace("-",
                                                        "").replace("Z", "")
    filename = f"{spec.trial_id}_{timestamp_tag}.json"

    artifact = {

        # ── Layer 1: Trial identity
        # trial_id uniquely identifies this specific execution instance.
        "trial_id": f"{spec.trial_id}_{timestamp_tag}",

        # ── Layer 2: Trial metadata
        "trial_metadata": {
            "fault_id": spec.fault_id,
            "fault_name": _fault_name(spec.fault_id),
            "condition": spec.condition,
            "repetition": spec.repetition,
            "started_at": trial_start_utc,
            "completed_at": trial_end_utc,
            "duration_seconds": _duration_s(trial_start_utc, trial_end_utc),
            # status reflects the trial outcome at the harness level.
            # "completed" means graph.invoke() returned without exception.
            # "error" is set by orchestrator.py if it catches an exception
            # and still manages to write a partial artifact.
            "status": "completed",
        },

        # ── Layer 3: Materialisation log
        # What the environment observed during fault materialisation.
        # Populated by environment.py — artifact.py writes it verbatim.
        "materialisation_log": {
            "strategy": materialisation_log.strategy,
            "elapsed_seconds": materialisation_log.elapsed_seconds,
            "poll_count": materialisation_log.poll_count,
            "satisfied_at_poll": materialisation_log.satisfied_at_poll,
            "final_signal_value": materialisation_log.final_signal_value,
            "threshold": materialisation_log.threshold,
            "notes": materialisation_log.notes,
        },

        # ── Layer 4: Full message list (raw) ──
        # Complete serialised message history for qualitative analysis
        # and full reproducibility. Nothing is thrown away.
        # Indexed so specific messages are easy to reference.
        "session": {
            "messages": _serialise_messages(messages),
        },

        # ── Layer 5: Session trace (derived summary) ──
        # All derived metrics about the session. RQ2 and RQ3 live here.
        "session_trace": session_summary,

        # ── Layer 6: Diagnosis
        # What the agent submitted via submit_diagnosis.
        # None fields mean agent did not submit (NS or SL outcome).
        "diagnosis": _build_diagnosis(submission, tool_trace),

        # ── Layer 7: Token usage (RQ4) ──
        "token_usage": token_usage,

        # ── Layer 8: Scoring (RQ1) ─
        "scoring": scoring,
    }

    artifact_path = output_dir / filename
    _write_atomic(artifact, artifact_path)

    logger.info(f"[artifact] Written: {filename} | "
                f"outcome={scoring['outcome_category']} | "
                f"exact_correct={scoring['exact_correct']} | "
                f"steps={step_count} | "
                f"termination={termination}")

    return artifact_path


# Extraction helpers


def _extract_submission(messages: list[BaseMessage]) -> Optional[dict]:
    """
    Find submit_diagnosis in the message list and return the agent's arguments.

    WHERE TO LOOK:
      The agent's submitted arguments are in AIMessage.tool_calls[i]["args"].
      NOT in the ToolMessage that follows (that only contains the tool's
      return value — whether the submission was accepted).
      We scan all AIMessages for a tool_call named "submit_diagnosis".

    Returns the args dict, or None if agent never called submit_diagnosis.
    e.g. {
      "service": "inventory-service",
      "component": "hikari-connection-pool",
      "fault_type": "connection-pool-starvation",
      "evidence": "...",
      "no_fault_detected": False,
    }
    """
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in msg.tool_calls:
            if tc["name"] == "submit_diagnosis":
                return tc["args"]
    return None


def _extract_tool_trace(messages: list[BaseMessage]) -> list[dict]:
    """
    Build an ordered list of every tool call made during the session.

    Each entry: {step_index, tool_name, call_id}
    step_index = which AIMessage turn produced this call (0-based).

    This directly answers RQ3: which tools were called, how many times,
    in what order, and at which reasoning step.
    """
    trace = []
    ai_turn = 0
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in msg.tool_calls:
            trace.append({
                "step_index": ai_turn,
                "tool_name": tc["name"],
                "call_id": tc["id"],
            })
        ai_turn += 1
    return trace


def _extract_token_usage(messages: list[BaseMessage]) -> dict:
    """
    Sum token usage across all AIMessages.

    LangChain populates AIMessage.usage_metadata when the model returns
    token counts. LM Studio may or may not do this — the flag
    token_tracking_available tells analysis scripts whether to trust the numbers.

    If token_tracking_available is False, all counts are null and RQ4
    cannot be answered from this trial.
    """
    total_input = 0
    total_output = 0
    found_any = False

    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        meta = msg.usage_metadata
        if meta and isinstance(meta, dict):
            # Handle both OpenAI naming conventions.
            inp = meta.get("input_tokens") or meta.get("prompt_tokens", 0) or 0
            out = meta.get("output_tokens") or meta.get(
                "completion_tokens", 0) or 0
            if inp or out:
                found_any = True
                total_input += inp
                total_output += out

    if not found_any:
        logger.warning(
            "[artifact] No token usage metadata in any AIMessage. "
            "LM Studio may not return usage. RQ4 will be null for this trial.")
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "estimated_cost_usd": None,
            "source": "unavailable",
        }

    total = total_input + total_output
    cost = ((total_input / 1000.0) * PRICE_PER_1K_INPUT_TOKENS +
            (total_output / 1000.0) * PRICE_PER_1K_OUTPUT_TOKENS)
    return {
        "prompt_tokens": total_input,
        "completion_tokens": total_output,
        "total_tokens": total,
        "estimated_cost_usd": round(cost, 6),
        "source": "llm_reported",
    }


def _determine_termination(terminated: bool,
                           messages: list[BaseMessage]) -> str:
    """
    Classify how the session ended. Three mutually exclusive outcomes:
    """
    if terminated:
        return "submit_diagnosis"
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)),
                   None)
    if last_ai is not None and not last_ai.tool_calls:
        return "step_limit"
    return "no_submission"


def _build_session_summary(
    messages: list[BaseMessage],
    step_count: int,
    termination: str,
    system_prompt: str,
    tool_trace: list[dict],
) -> dict:
    """
    Build the session_trace block — all derived session-level metrics.
    Corresponds directly to RQ2 (step_count) and RQ3 (tool usage).
    """
    tool_names = [t["tool_name"] for t in tool_trace]
    unique_tools = list(dict.fromkeys(tool_names))  # ordered, deduplicated

    # Count occurrences of each tool name.
    tool_counts: dict[str, int] = {}
    for name in tool_names:
        tool_counts[name] = tool_counts.get(name, 0) + 1

    most_called = max(tool_counts,
                      key=tool_counts.get) if tool_counts else None
    most_called_count = tool_counts[most_called] if most_called else 0

    return {
        "total_messages": len(messages),
        "total_steps": step_count,
        "total_tool_calls": len(tool_trace),
        "unique_tools_called": unique_tools,
        "tool_call_counts": tool_counts,
        "most_called_tool": most_called,
        "most_called_tool_count": most_called_count,
        "terminated_by_agent": termination == "submit_diagnosis",
        "step_limit_reached": termination == "step_limit",
        "termination_reason": termination,
        "system_prompt_snapshot": system_prompt,
    }


def _build_diagnosis(
    submission: Optional[dict],
    tool_trace: list[dict],
) -> dict:
    """
    Build the diagnosis block from the agent's submission.

    step_index: which step the submit_diagnosis call was made at.
    Derived from tool_trace — find the entry with tool_name == "submit_diagnosis".
    """
    if submission is None:
        return {
            "submitted": False,
            "service": None,
            "component": None,
            "fault_type": None,
            "no_fault_detected": None,
            "evidence": None,
            "step_index": None,
        }

    submit_step = next(
        (t["step_index"]
         for t in tool_trace if t["tool_name"] == "submit_diagnosis"),
        None,
    )

    return {
        "submitted": True,
        "service": submission.get("service"),
        "component": submission.get("component"),
        "fault_type": submission.get("fault_type"),
        "no_fault_detected": submission.get("no_fault_detected", False),
        "evidence": submission.get("evidence"),
        "step_index": submit_step,
    }


def _score(
    submission: Optional[dict],
    ground_truth: GroundTruth,
    termination: str,
) -> dict:
    """
    Score the agent's submission against ground truth.

    THREE INDEPENDENT BOOLEAN METRICS:
      service_correct, component_correct, fault_type_correct.
      Scored independently so partial credit analysis is possible.

    COMPOSITE METRIC:
      exact_correct = True only if ALL THREE are correct.
      This is the primary RQ1 metric.

    PARTIAL SCORE:
      Float 0.0–1.0 = (number correct) / 3.
      Allows finer-grained accuracy analysis than a binary correct/wrong.

    OUTCOME CATEGORY:
      EC  — Exact Correct (all three match)
      PC  — Partial Correct (at least one match, not all)
      WD  — Wrong Diagnosis (submitted, all three wrong)
      NFD — No Fault Detected (agent declared no fault)
      NS  — No Submission (agent never submitted)
      SL  — Step Limit reached without submission
    """
    gt = {
        "service": ground_truth.service,
        "component": ground_truth.component,
        "fault_type": ground_truth.fault_type,
    }

    # Handle no-submission outcomes first.
    if submission is None:
        outcome = OUTCOME_SL if termination == "step_limit" else OUTCOME_NS
        return {
            "ground_truth": gt,
            "service_correct": None,
            "component_correct": None,
            "fault_type_correct": None,
            "exact_correct": False,
            "partial_score": 0.0,
            "outcome_category": outcome,
        }

    # Agent declared no fault — wrong, fault was real and active.
    if submission.get("no_fault_detected") is True:
        return {
            "ground_truth": gt,
            "service_correct": False,
            "component_correct": False,
            "fault_type_correct": False,
            "exact_correct": False,
            "partial_score": 0.0,
            "outcome_category": OUTCOME_NFD,
        }

    # Normal scoring path.
    service_correct = submission.get("service") == ground_truth.service
    component_correct = submission.get("component") == ground_truth.component
    fault_type_correct = submission.get(
        "fault_type") == ground_truth.fault_type
    exact_correct = service_correct and component_correct and fault_type_correct

    correct_count = sum(
        [service_correct, component_correct, fault_type_correct])
    partial_score = round(correct_count / 3.0, 4)

    if exact_correct:
        outcome = OUTCOME_EC
    elif correct_count > 0:
        outcome = OUTCOME_PC
    else:
        outcome = OUTCOME_WD

    return {
        "ground_truth": gt,
        "service_correct": service_correct,
        "component_correct": component_correct,
        "fault_type_correct": fault_type_correct,
        "exact_correct": exact_correct,
        "partial_score": partial_score,
        "outcome_category": outcome,
    }


# Message serialisation


def _serialise_messages(messages: list[BaseMessage]) -> list[dict]:
    """
    Serialise the full message list to plain dicts for JSON storage.
    Indexed so any message can be referenced by position.

    We write only the fields needed for analysis — not message.dict()
    which includes LangChain internals and bloats the file.
    """
    result = []
    for i, msg in enumerate(messages):
        entry: dict[str, Any] = {
            "index": i,
            "type": _msg_type(msg),
            "content": msg.content,
        }
        if isinstance(msg, AIMessage):
            entry["tool_calls"] = msg.tool_calls
            entry["usage_metadata"] = msg.usage_metadata
        if isinstance(msg, ToolMessage):
            entry["tool_call_id"] = msg.tool_call_id
            entry["name"] = getattr(msg, "name", None)
        result.append(entry)
    return result


def _msg_type(msg: BaseMessage) -> str:
    if isinstance(msg, SystemMessage): return "SystemMessage"
    if isinstance(msg, HumanMessage): return "HumanMessage"
    if isinstance(msg, AIMessage): return "AIMessage"
    if isinstance(msg, ToolMessage): return "ToolMessage"
    return "UnknownMessage"


# File I/O — atomic write


def _write_atomic(doc: dict, path: Path) -> None:
    """
    Write JSON atomically. Tmp file → rename.
    Either old content or new content on disk — never a partial write.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(path)


# Helpers


def _fault_name(fault_id: str) -> str:
    """Map fault_id to the human-readable fault_type string from ground truth."""
    from harness.fault_catalogue import get_ground_truth
    try:
        return get_ground_truth(fault_id).fault_type
    except KeyError:
        return "unknown"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration_s(start_utc: str, end_utc: str) -> Optional[float]:
    """Compute wall-clock duration in seconds between two ISO 8601 strings."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        start = datetime.strptime(start_utc, fmt).replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_utc, fmt).replace(tzinfo=timezone.utc)
        return round((end - start).total_seconds(), 1)
    except (ValueError, TypeError):
        return None
