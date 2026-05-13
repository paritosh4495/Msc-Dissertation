#
# fault_trial.py
#
# Orchestrated fault-injection trial runner.
#
# Workflow:
#   1. Activate a chosen fault (F1–F6) on the correct service
#   2. Wait for it to materialise (per-fault logic — see _materialise())
#   3. Run Condition A agent → dump results to JSON
#   4. Deactivate fault
#   5. Restart ALL pods for a clean slate
#   6. Wait for all pods to become Ready again
#   7. Extra 30s JVM warmup wait
#   8. Re-activate the same fault
#   9. Wait for materialisation again (per-fault logic)
#  10. Run Condition B agent → dump results to JSON
#  11. Deactivate fault (cleanup)
#
# Per-fault materialisation behaviour:
#   F1–F3 : uniform materialise_wait (default 30s, overridable via --materialize-wait)
#   F4    : launches concurrent load workers THEN waits 90s for thread pool saturation
#   F5    : waits 150s (Cond A) or polls heap until >60% (Cond B) for leak to accumulate
#   F6    : polls kubectl restart_count until OOMKill is confirmed, starts agent immediately
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
import threading
import time
import urllib.request
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
    JVM_HEAP_MAX_BYTES,
    NAMESPACE,
    ORDER_BASE_URL,
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

# Per-fault materialise_wait overrides (seconds).
# Applied in _materialise() for F4 and F5.
# F6 ignores this entirely — it uses event-driven polling.
FAULT_MATERIALIZE_OVERRIDES: dict[str, int] = {
    "f4": 90,  # thread pool saturation needs sustained concurrent load + time
    "f5": 150,  # slow heap leak must accumulate past GC recovery threshold
}

# F4 concurrent worker count.
# getBlockLimit() = maxThreads - max(2, maxThreads/4).
# With default 200 Tomcat threads: limit = 150.
# 60 workers is sufficient to saturate any realistic configuration
# without overwhelming Minikube host resources during LLM inference.
F4_LOAD_CONCURRENCY = 60

# F5 heap threshold (Condition B only).
# Baseline heap under light load is ~30–40% of -Xmx512m.
# 60% confirms meaningful leak accumulation above baseline.
F5_HEAP_THRESHOLD_PCT = 60.0

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
# F6 — OOMKill detection gate
# ---------------------------------------------------------------------------


def wait_for_f6_oomkill(
    namespace: str = NAMESPACE,
    service: str = "inventory-service",
    timeout_seconds: int = 120,
    poll_interval: int = 3,
) -> None:
    """
    For F6: poll kubectl until restart_count > 0 on the inventory-service pod,
    then return immediately so the agent starts while the OOMKill event is
    fresh in the Kubernetes event log and the pod is still in its recovery window.

    Uses kubectl jsonpath directly — more reliable than hitting the service's
    own HTTP endpoint during a pod restart when it may be briefly unreachable.

    Note: MaxDirectMemorySize=1g exceeds the pod memory limit of 850Mi, so
    OOMKill typically fires within 15–30s of fault activation. The 120s
    timeout is conservative.
    """
    print(
        f"\n[f6-wait] Polling for OOMKill on {service} (timeout={timeout_seconds}s)..."
    )
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            result = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-n",
                    namespace,
                    "-l",
                    f"app={service}",
                    "-o",
                    "jsonpath={.items[0].status.containerStatuses[0].restartCount}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            restart_count = int(result.stdout.strip() or "0")
            if restart_count > 0:
                print(
                    f"       ✓ OOMKill confirmed — restart_count={restart_count}"
                )
                print(
                    f"       Starting agent immediately while event is fresh.")
                return
        except Exception:
            # Pod may be briefly unreachable mid-restart — expected, not an error
            pass
        remaining = int(deadline - time.time())
        print(f"       ... restart_count=0 still ({remaining}s remaining)")
        time.sleep(poll_interval)
    raise RuntimeError(
        f"F6: OOMKill did not occur within {timeout_seconds}s. "
        "Check fault injection endpoint and MaxDirectMemorySize config.")


# ---------------------------------------------------------------------------
# F5 — Heap pressure gate (Condition B only)
# ---------------------------------------------------------------------------


def wait_for_f5_heap_pressure(
    threshold_pct: float = F5_HEAP_THRESHOLD_PCT,
    timeout_seconds: int = 180,
    poll_interval: int = 10,
) -> None:
    """
    For F5 Condition B: polls the Actuator jvm.memory.used (heap area) metric
    directly from the harness until heap exceeds threshold_pct of JVM_HEAP_MAX_BYTES.

    This ensures the agent starts only after the leak has accumulated a
    measurable and observable delta above the GC recovery baseline.

    Falls back gracefully — if the threshold is not reached within timeout,
    logs a warning and starts the agent anyway rather than raising.

    Only used for Condition B because the harness has direct Actuator access
    via NodePort. Condition A uses the fixed FAULT_MATERIALIZE_OVERRIDES wait.
    """
    from config import ACTUATOR_NODE_PORTS, ACTUATOR_BASE_HOST

    port = ACTUATOR_NODE_PORTS["inventory-service"]
    url = (f"http://{ACTUATOR_BASE_HOST}:{port}"
           "/actuator/metrics/jvm.memory.used?tag=area:heap")
    print(
        f"\n[f5-wait] Polling heap until >{threshold_pct}% "
        f"(JVM_HEAP_MAX={JVM_HEAP_MAX_BYTES // (1024*1024)}MB, timeout={timeout_seconds}s)..."
    )
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            used_bytes = data["measurements"][0]["value"]
            pct = round((used_bytes / JVM_HEAP_MAX_BYTES) * 100, 1)
            print(f"       heap at {pct}% — target {threshold_pct}%")
            if pct >= threshold_pct:
                print(f"       ✓ Heap threshold reached. Starting agent.")
                return
        except Exception as e:
            print(
                f"       ... poll failed ({e}), retrying in {poll_interval}s")
        time.sleep(poll_interval)
    print(
        f"       WARNING: Heap did not reach {threshold_pct}% within {timeout_seconds}s. "
        "Starting agent anyway — trial data may show weak fault signal.")


# ---------------------------------------------------------------------------
# F4 — Concurrent load generator
# ---------------------------------------------------------------------------


def _f4_load_worker(stop_event: threading.Event) -> None:
    """
    Single background worker for F4 load.
    Sends continuous GET requests to inventory-service /api/products.
    Each in-flight request claims one Tomcat thread and, once the fault
    trap activates, holds it in lock.wait() inside F4ThreadPoolExhaustionFault.
    Uses a long timeout so threads stay blocked rather than timing out.
    """
    url = f"{INVENTORY_BASE_URL}/api/products"
    while not stop_event.is_set():
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=120):
                pass
        except Exception:
            # Timeouts and connection errors are expected once pool is saturated
            pass


def start_f4_load(concurrency: int = F4_LOAD_CONCURRENCY) -> threading.Event:
    """
    Launches `concurrency` background threads, each sending continuous requests
    to inventory-service. Returns the stop_event so the caller can halt them.

    F4ThreadPoolExhaustionFault.getBlockLimit() = maxThreads - max(2, maxThreads/4).
    With default 200 Tomcat threads the limit is 150.
    60 concurrent workers saturates any realistic thread pool configuration
    without overwhelming Minikube host CPU during LLM inference.
    """
    stop_event = threading.Event()
    print(f"\n[f4-load] Launching {concurrency} concurrent load workers...")
    for _ in range(concurrency):
        t = threading.Thread(
            target=_f4_load_worker,
            args=(stop_event, ),
            daemon=True,
        )
        t.start()
    print(f"[f4-load] {concurrency} workers running.")
    return stop_event


def stop_f4_load(stop_event: threading.Event) -> None:
    print("\n[f4-load] Stopping load workers...")
    stop_event.set()
    # Daemon threads — they will exit on their own once stop_event is set.
    # Brief pause to let them wind down before deactivation.
    time.sleep(2)
    print("[f4-load] Load workers stopped.")


# ---------------------------------------------------------------------------
# Per-fault materialisation dispatcher
# ---------------------------------------------------------------------------


def _materialise(
    fault_id: str,
    default_wait: int,
    condition: str,
    label: str,
) -> None:
    """
    Central materialisation gate. Dispatches to fault-specific logic where
    needed; falls back to uniform materialise_wait for F1–F3.

    Args:
        fault_id:     e.g. "f4"
        default_wait: the --materialize-wait CLI value (used for F1–F3)
        condition:    "A" or "B" (F5 uses different logic per condition)
        label:        human-readable label for log output
    """
    if fault_id == "f6":
        # Event-driven: poll until OOMKill is confirmed, then start immediately.
        wait_for_f6_oomkill()

    elif fault_id == "f5":
        if condition == "B":
            # Condition B has direct Actuator access — use heap threshold gate.
            wait_for_f5_heap_pressure()
        else:
            # Condition A: no Actuator access from harness — use extended fixed wait.
            effective = FAULT_MATERIALIZE_OVERRIDES.get("f5", default_wait)
            materialise_wait(effective, label)

    elif fault_id == "f4":
        # F4 uses a fixed extended wait (load workers are already running).
        effective = FAULT_MATERIALIZE_OVERRIDES.get("f4", default_wait)
        materialise_wait(effective, label)

    else:
        # F1, F2, F3: uniform wait, honouring the CLI --materialize-wait value.
        materialise_wait(default_wait, label)


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
    timestamp = datetime.now(timezone.utc).strftime("%Y_%m_%d__%H_%M_%S")

    date_str = datetime.now().strftime("%Y_%m_%d")
    output_dir = output_dir / f"trial_results_{date_str}"

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

    # F4: start concurrent load BEFORE activating the fault so threads are
    # already in-flight when the trap activates and can be immediately caught.
    f4_stop_a: threading.Event | None = None
    if fault_id == "f4":
        f4_stop_a = start_f4_load()

    activate_fault(fault_id)
    _materialise(fault_id, materialize_wait, "A", f"{fault_id} → Condition A")

    t_start_a = time.time()
    state_a = run_agent("A")
    elapsed_a = time.time() - t_start_a

    if f4_stop_a is not None:
        stop_f4_load(f4_stop_a)

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

    f4_stop_b: threading.Event | None = None
    if fault_id == "f4":
        f4_stop_b = start_f4_load()

    activate_fault(fault_id)
    _materialise(fault_id, materialize_wait, "B", f"{fault_id} → Condition B")

    t_start_b = time.time()
    state_b = run_agent("B")
    elapsed_b = time.time() - t_start_b

    if f4_stop_b is not None:
        stop_f4_load(f4_stop_b)

    dump_trial_to_json(state_b, "B", fault_id, fault_meta, elapsed_b, path_b)

    # ── FINAL CLEANUP ───────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("FINAL CLEANUP")
    print(f"{'='*70}")
    deactivate_fault(fault_id)

    # ── SUMMARY ─────────────────────────────────────────────────────────
    print(f"\n{SEP2}")
    print("TRIAL COMPLETE")
    print(f"  Fault      : {fault_id.upper()} — {description}")
    print(f"  Condition A: {path_a}")
    print(f"  Condition B: {path_b}")
    print(SEP2)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a fault-injection trial for the diagnostic agent.")
    parser.add_argument(
        "--fault",
        required=True,
        choices=sorted(FAULT_CATALOGUE),
        help="Fault ID to inject (f1–f6)",
    )
    parser.add_argument(
        "--materialize-wait",
        type=int,
        default=30,
        help=(
            "Seconds to wait after fault activation before starting the agent "
            "(default: 30). Applied to F1–F3. F4/F5/F6 use per-fault logic "
            "and ignore this value."),
    )
    parser.add_argument(
        "--pod-ready-wait",
        type=int,
        default=180,
        help=
        "Timeout (s) for waiting for pods to become Ready after restart (default: 180)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("trial_results"),
        help=
        "Directory to write trial JSON output files (default: trial_results/)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_trial(
        fault_id=args.fault,
        materialize_wait=args.materialize_wait,
        pod_ready_wait=args.pod_ready_wait,
        output_dir=args.output_dir,
    )
