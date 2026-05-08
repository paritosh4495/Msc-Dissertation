#
# fault_trial.py
#
# Orchestrated fault-injection trial runner.
#
# Workflow:
#   1. Activate a chosen fault (F1–F6) on the correct service
#   2. Wait for it to materialise (configurable, default 30s)
#   3. Run Condition A agent → dump results to JSON
#   4. Deactivate fault
#   5. Restart ALL pods for a clean slate
#   6. Wait for all pods to become Ready again
#   7. Extra 30s JVM warmup wait
#   8. Re-activate the same fault
#   9. Wait for materialisation again
#  10. Run Condition B agent → dump results to JSON
#  11. Deactivate fault (cleanup)
#
# Output files (written to trial_results/ by default):
#   trial_<faultId>_conditionA_<timestamp>.json
#   trial_<faultId>_conditionB_<timestamp>.json
#
# Run from Agent_Impl/:
#   python fault_trial.py --fault f3
#   python fault_trial.py --fault f1 --materialize-wait 45 --pod-ready-wait 180
#

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# ---------------------------------------------------------------------------
# Logging — mirrors smoke_test.py convention
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
for _logger_name in ("agent.nodes", "agent.graph", "tools"):
    logging.getLogger(_logger_name).setLevel(logging.DEBUG)

from agent import GRAPH_RECURSION_LIMIT, build_agent, build_initial_state
from config import (
    AGENT_STEP_LIMIT,
    INVENTORY_BASE_URL,
    ORDER_BASE_URL,
    NAMESPACE,
    PAYMENT_BASE_URL,
)

# ---------------------------------------------------------------------------
# Fault catalogue
# fault_id -> (service_name, base_url, human description)
# ---------------------------------------------------------------------------
FAULT_CATALOGUE: dict[str, tuple[str, str, str]] = {
    "f1": ("inventory-service", INVENTORY_BASE_URL,
           "Hikari connection pool starvation"),
    "f2": ("inventory-service", INVENTORY_BASE_URL,
           "CPU saturation via worker threads"),
    "f3": ("payment-service", PAYMENT_BASE_URL,
           "Forced payment authorisation failures (HTTP 500)"),
    "f4":
    ("inventory-service", INVENTORY_BASE_URL, "Tomcat thread pool exhaustion"),
    "f5": ("inventory-service", INVENTORY_BASE_URL, "Slow heap memory leak"),
    "f6": ("inventory-service", INVENTORY_BASE_URL,
           "Off-heap spike → Kubernetes OOMKill"),
}

SEP = "─" * 70
SEP2 = "═" * 70

# ---------------------------------------------------------------------------
# Fault injection helpers
# ---------------------------------------------------------------------------


def _fault_url(base_url: str, fault_id: str, action: str) -> str:
    return f"{base_url}/internal/fault/{action}/{fault_id}"


def activate_fault(fault_id: str) -> None:
    service_name, base_url, description = FAULT_CATALOGUE[fault_id]
    url = _fault_url(base_url, fault_id, "activate")
    print(f"\n[fault] Activating {fault_id} on {service_name}: {description}")
    print(f"        POST {url}")
    resp = requests.post(url, timeout=10)
    resp.raise_for_status()
    print(f"        → HTTP {resp.status_code}  |  {resp.text[:120]}")


def deactivate_fault(fault_id: str) -> None:
    service_name, base_url, _ = FAULT_CATALOGUE[fault_id]
    url = _fault_url(base_url, fault_id, "deactivate")
    print(f"\n[fault] Deactivating {fault_id} on {service_name}")
    print(f"        POST {url}")
    try:
        resp = requests.post(url, timeout=10)
        resp.raise_for_status()
        print(f"        → HTTP {resp.status_code}  |  {resp.text[:120]}")
    except Exception as exc:
        # Non-fatal — pod may already be restarting (e.g. f6 OOMKill)
        print(
            f"        → WARNING: deactivate call failed ({exc}). Continuing.")


# ---------------------------------------------------------------------------
# Pod restart + readiness helpers
# ---------------------------------------------------------------------------


def restart_all_pods(namespace: str = NAMESPACE) -> None:
    deployments = ["inventory-service", "order-service", "payment-service"]
    print(f"\n[pods] Restarting all deployments in namespace '{namespace}'...")
    for dep in deployments:
        cmd = [
            "kubectl", "rollout", "restart", f"deployment/{dep}", "-n",
            namespace
        ]
        print(f"       $ {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"       WARNING: {result.stderr.strip()}")
        else:
            print(f"       → {result.stdout.strip()}")


def wait_for_all_pods_ready(
    namespace: str = NAMESPACE,
    timeout_seconds: int = 180,
    poll_interval: int = 5,
) -> None:
    deployments = ["inventory-service", "order-service", "payment-service"]
    print(
        f"\n[pods] Waiting for all pods to be Ready (timeout={timeout_seconds}s)..."
    )
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        all_ready = True
        for dep in deployments:
            cmd = [
                "kubectl",
                "rollout",
                "status",
                f"deployment/{dep}",
                "-n",
                namespace,
                "--timeout=10s",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                all_ready = False
                break
        if all_ready:
            print("       ✓ All deployments rolled out and ready.")
            return
        remaining = int(deadline - time.time())
        print(f"       ... not ready yet — retrying (≤{remaining}s remaining)")
        time.sleep(poll_interval)
    raise RuntimeError(
        f"Timed out after {timeout_seconds}s waiting for pods to become ready."
    )


def materialise_wait(seconds: int, label: str) -> None:
    print(f"\n[wait] Pausing {seconds}s for '{label}' to materialise...")
    for remaining in range(seconds, 0, -5):
        print(f"       {remaining}s remaining...")
        time.sleep(min(5, remaining))
    print("       ✓ Done.")


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------


def run_agent(condition: str) -> dict:
    print(f"\n{SEP2}")
    print(f"AGENT RUN — Condition {condition}")
    print(f"Step limit : {AGENT_STEP_LIMIT}")
    print(f"Recursion  : {GRAPH_RECURSION_LIMIT}")
    print(SEP2)

    print("\n  Building agent graph...")
    t0 = time.time()
    graph, system_prompt = build_agent(condition)
    print(f"  Graph compiled in {time.time() - t0:.2f}s")

    print("  Building initial state...")
    initial_state = build_initial_state(condition, system_prompt)
    print(f"  Initial messages : {len(initial_state['messages'])}")
    print(f"  Condition        : {initial_state['condition']}")
    print(f"  Step count       : {initial_state['step_count']}")
    print(f"  Terminated       : {initial_state['terminated']}")

    print(f"\n  Invoking graph (live LLM + K8s calls)...")
    print(SEP)

    t1 = time.time()
    try:
        final_state = graph.invoke(
            initial_state,
            config={"recursion_limit": GRAPH_RECURSION_LIMIT},
        )
    except Exception as exc:
        print(f"\n✗ AGENT RUN FAILED — unhandled exception:")
        print(f"  {type(exc).__name__}: {exc}")
        raise

    elapsed = time.time() - t1
    print(f"\n  Graph execution completed in {elapsed:.1f}s")
    print(f"  step_count : {final_state['step_count']}")
    print(f"  terminated : {final_state['terminated']}")
    print(f"  messages   : {len(final_state['messages'])} total")
    return final_state


# ---------------------------------------------------------------------------
# JSON serialisation — mirrors smoke_test.py
# ---------------------------------------------------------------------------


def _serialise_message(msg) -> dict:
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
            "id": tc["id"]
        } for tc in (msg.tool_calls or [])]
    if isinstance(msg, ToolMessage):
        base["tool_call_id"] = msg.tool_call_id
        base["name"] = msg.name
    return base


def dump_trial_to_json(
    state: dict,
    condition: str,
    fault_id: str,
    fault_meta: dict,
    elapsed_s: float,
    path: Path,
) -> None:
    output = {
        "trial_meta": {
            "fault_id": fault_id,
            "fault_service": fault_meta["service"],
            "fault_description": fault_meta["description"],
            "condition": condition,
            "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed_s, 1),
            "step_limit": AGENT_STEP_LIMIT,
        },
        "outcome": {
            "step_count": state.get("step_count"),
            "terminated": state.get("terminated"),
            "total_messages": len(state.get("messages", [])),
        },
        "messages": [_serialise_message(m) for m in state.get("messages", [])],
    }
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\n[output] Session saved → {path}")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_trial(
    fault_id: str,
    materialize_wait: int,
    pod_ready_wait: int,
    output_dir: Path,
) -> None:
    if fault_id not in FAULT_CATALOGUE:
        print(
            f"ERROR: Unknown fault '{fault_id}'. Valid: {sorted(FAULT_CATALOGUE)}"
        )
        sys.exit(1)

    service_name, _, description = FAULT_CATALOGUE[fault_id]
    fault_meta = {"service": service_name, "description": description}
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    output_dir.mkdir(parents=True, exist_ok=True)
    path_a = output_dir / f"trial_{fault_id}_conditionA_{timestamp}.json"
    path_b = output_dir / f"trial_{fault_id}_conditionB_{timestamp}.json"

    print(SEP2)
    print(f"FAULT TRIAL  —  Fault: {fault_id.upper()}  |  {description}")
    print(f"Service      : {service_name}")
    print(f"Conditions   : A then B")
    print(f"Output dir   : {output_dir.resolve()}")
    print(SEP2)

    # ── PHASE 1: Condition A ────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("PHASE 1 / 2  —  Condition A")
    print(f"{'='*70}")

    activate_fault(fault_id)
    materialise_wait(materialize_wait, f"{fault_id} → Condition A")

    t_start_a = time.time()
    state_a = run_agent("A")
    elapsed_a = time.time() - t_start_a

    dump_trial_to_json(state_a, "A", fault_id, fault_meta, elapsed_a, path_a)

    # ── INTER-PHASE CLEANUP ─────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("INTER-PHASE CLEANUP")
    print(f"{'='*70}")

    deactivate_fault(fault_id)
    restart_all_pods(NAMESPACE)
    wait_for_all_pods_ready(NAMESPACE, timeout_seconds=pod_ready_wait)
    materialise_wait(30, "post-restart JVM warmup")

    # ── PHASE 2: Condition B ────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("PHASE 2 / 2  —  Condition B")
    print(f"{'='*70}")

    activate_fault(fault_id)
    materialise_wait(materialize_wait, f"{fault_id} → Condition B")

    t_start_b = time.time()
    state_b = run_agent("B")
    elapsed_b = time.time() - t_start_b

    dump_trial_to_json(state_b, "B", fault_id, fault_meta, elapsed_b, path_b)

    # ── FINAL CLEANUP ───────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("FINAL CLEANUP")
    print(f"{'='*70}")
    deactivate_fault(fault_id)

    # ── SUMMARY ─────────────────────────────────────────────────────────
    print(f"\n{SEP2}")
    print("TRIAL COMPLETE")
    print(SEP2)
    print(f"  Fault       : {fault_id.upper()} — {description}")
    print(f"  Condition A : {state_a['step_count']} steps | "
          f"terminated={state_a['terminated']} | elapsed={elapsed_a:.1f}s")
    print(f"  Condition B : {state_b['step_count']} steps | "
          f"terminated={state_b['terminated']} | elapsed={elapsed_b:.1f}s")
    print(f"  Output A    : {path_a}")
    print(f"  Output B    : {path_b}")
    print(SEP2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=
        "Fault injection trial runner — injects a fault, runs both conditions, saves results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Fault catalogue:
  f1  inventory-service  Hikari connection pool starvation
  f2  inventory-service  CPU saturation via worker threads
  f3  payment-service    Forced payment authorisation failures (HTTP 500)
  f4  inventory-service  Tomcat thread pool exhaustion
  f5  inventory-service  Slow heap memory leak
  f6  inventory-service  Off-heap spike -> Kubernetes OOMKill

Examples:
  python fault_trial.py --fault f3
  python fault_trial.py --fault f1 --materialize-wait 45 --pod-ready-wait 180
        """,
    )
    parser.add_argument(
        "--fault",
        required=True,
        choices=sorted(FAULT_CATALOGUE.keys()),
        help="Fault ID to inject (f1–f6)",
    )
    parser.add_argument(
        "--materialize-wait",
        type=int,
        default=30,
        metavar="SECONDS",
        help=
        "Seconds to wait after activating fault before running the agent (default: 30)",
    )
    parser.add_argument(
        "--pod-ready-wait",
        type=int,
        default=180,
        metavar="SECONDS",
        help=
        "Max seconds to wait for all pods Ready after restart (default: 180)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("trial_results"),
        metavar="DIR",
        help="Directory to write JSON output files (default: ./trial_results/)",
    )
    args = parser.parse_args()

    run_trial(
        fault_id=args.fault,
        materialize_wait=args.materialize_wait,
        pod_ready_wait=args.pod_ready_wait,
        output_dir=args.output_dir,
    )
