# End-to-end smoke test for the diagnostic agent.
# Runs a single trial against the testbed to verify the LLM-to-tool loop.

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pathlib import Path

# Initialise logging before importing agent components
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)

# Enable debug logging for the agent and tools
for logger_name in ("agent.nodes", "agent.graph", "tools"):
    logging.getLogger(logger_name).setLevel(logging.DEBUG)

from agent import GRAPH_RECURSION_LIMIT, build_agent, build_initial_state
from config import AGENT_STEP_LIMIT

# Display constants
SEP = "─" * 70
SEP2 = "═" * 70


def _print_message(msg, index: int):
    """Pretty-prints LangChain messages for terminal output."""
    if isinstance(msg, SystemMessage):
        print(
            f"  [{index}] SystemMessage ({len(str(msg.content))} chars) — system prompt"
        )

    elif isinstance(msg, HumanMessage):
        print(f"  [{index}] HumanMessage: {str(msg.content)[:120]}")

    elif isinstance(msg, AIMessage):
        tool_calls = msg.tool_calls or []
        if tool_calls:
            calls_str = ", ".join(f"{tc['name']}({list(tc['args'].keys())})"
                                  for tc in tool_calls)
            print(f"  [{index}] AIMessage → tool calls: {calls_str}")
        else:
            content_preview = str(msg.content)[:200]
            print(f"  [{index}] AIMessage (no tool calls): {content_preview}")

    elif isinstance(msg, ToolMessage):
        try:
            parsed = json.loads(msg.content)
            status = parsed.get("status", "?")
            data_keys = list(parsed.get("data", {}).keys())
            print(f"  [{index}] ToolMessage [{status}] data keys: {data_keys}")
        except (json.JSONDecodeError, AttributeError):
            print(f"  [{index}] ToolMessage: {str(msg.content)[:120]}")

    else:
        print(f"  [{index}] {type(msg).__name__}: {str(msg.content)[:120]}")


def _print_final_state(final_state: dict):
    """Outputs the summary of the final agent state."""
    print(SEP2)
    print("FINAL STATE")
    print(SEP2)
    print(f"  condition   : {final_state['condition']}")
    print(f"  step_count  : {final_state['step_count']}")
    print(f"  terminated  : {final_state['terminated']}")
    print(f"  messages    : {len(final_state['messages'])} total")
    print()
    print("MESSAGE TRACE:")
    for i, msg in enumerate(final_state["messages"]):
        _print_message(msg, i)
    print(SEP2)


def _determine_outcome(final_state: dict) -> tuple[str, bool]:
    """Evaluates the outcome of the test run for reporting."""
    if final_state["terminated"]:
        return "SUBMITTED_DIAGNOSIS", True

    last_msg = final_state["messages"][-1]
    if isinstance(last_msg, AIMessage) and not last_msg.tool_calls:
        return "NO_TOOL_CALLS", True

    if final_state["step_count"] >= AGENT_STEP_LIMIT:
        return "STEP_LIMIT_REACHED", True

    return "UNKNOWN_TERMINATION", False


def run_smoke_test(condition: str):
    """Executes the agent loop for the given observability condition."""
    print(SEP2)
    print(f"SMOKE TEST — Condition {condition}")
    print(f"Step limit : {AGENT_STEP_LIMIT}")
    print(f"Recursion  : {GRAPH_RECURSION_LIMIT}")
    print(SEP2)

    # Initialise agent
    print("\n[1/3] Building agent graph...")
    t0 = time.time()
    graph, system_prompt = build_agent(condition)
    print(f"      Graph compiled in {time.time() - t0:.2f}s")

    # Set initial state
    print("\n[2/3] Building initial state...")
    initial_state = build_initial_state(condition, system_prompt)
    print(f"      Condition: {initial_state['condition']}")

    # Start the diagnostic loop
    print(f"\n[3/3] Invoking graph (making live LLM and K8s calls)...")
    print(SEP)

    t1 = time.time()
    try:
        final_state = graph.invoke(
            initial_state,
            config={"recursion_limit": GRAPH_RECURSION_LIMIT},
        )
    except Exception as e:
        print(
            f"\n✗ SMOKE TEST FAILED — unhandled exception during graph.invoke():"
        )
        print(f"  {type(e).__name__}: {e}")
        raise

    elapsed = time.time() - t1
    print(f"\nGraph execution completed in {elapsed:.1f}s")

    _print_final_state(final_state)
    outcome, passed = _determine_outcome(final_state)

    print()
    if passed:
        print(f"✓ SMOKE TEST PASSED — outcome: {outcome}")
    else:
        print(f"✗ SMOKE TEST FAILED — outcome: {outcome}")
        sys.exit(1)

    return final_state


def dump_session_to_json(state: dict,
                         condition: str,
                         path: str = "smoke_output.json") -> None:
    """Exports the session messages to JSON for manual review."""

    def serialise_message(msg) -> dict:
        base = {
            "type":
            type(msg).__name__,
            "content":
            msg.content if isinstance(msg.content, str) else str(msg.content),
        }
        if isinstance(msg, AIMessage):
            base["tool_calls"] = [{
                "name": tc["name"],
                "args": tc["args"],
                "id": tc["id"],
            } for tc in (msg.tool_calls or [])]
        if isinstance(msg, ToolMessage):
            base["tool_call_id"] = msg.tool_call_id
            base["name"] = msg.name
        return base

    output = {
        "condition": condition,
        "step_count": state.get("step_count"),
        "terminated": state.get("terminated"),
        "total_messages": len(state.get("messages", [])),
        "messages": [serialise_message(m) for m in state.get("messages", [])],
    }

    Path(path).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\n[smoke] Session dumped → {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="End-to-end agent smoke test")
    parser.add_argument(
        "--condition",
        choices=["A", "B"],
        default="B",
        help="Observability condition to test (default: B)",
    )
    args = parser.parse_args()
    final_state = run_smoke_test(args.condition)
    dump_session_to_json(final_state, args.condition, path="smoke_output.json")
