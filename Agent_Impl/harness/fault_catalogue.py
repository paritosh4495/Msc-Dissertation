# Literal strings in GroundTruth.component and GroundTruth.fault_type
# must exactly match the Literal types in tools/common_tools.py:submit_diagnosis.
# Any mismatch means the agent can never score a correct diagnosis for that fault.

from __future__ import annotations

from dataclasses import dataclass
from config import INVENTORY_BASE_URL, ORDER_BASE_URL, PAYMENT_BASE_URL

# Fault catalogue


@dataclass(frozen=True)
class FaultSpec:
    """
    service:     K8s label app=<service> — used for rollout status waits,
                 restart_count checks, and log references.
    base_url:    NodePort base URL for fault injection endpoint.
                 POST {base_url}/internal/fault/{activate|deactivate}/{fault_id}
    description: Human-readable label used in logging and artifact metadata.
    """
    fault_id: str
    service: str
    base_url: str
    description: str


FAULT_CATALOGUE: dict[str, FaultSpec] = {
    "f1":
    FaultSpec(
        fault_id="f1",
        service="inventory-service",
        base_url=INVENTORY_BASE_URL,
        description="Hikari connection pool starvation",
    ),
    "f2":
    FaultSpec(
        fault_id="f2",
        service="inventory-service",
        base_url=INVENTORY_BASE_URL,
        description="CPU saturation via worker threads",
    ),
    "f3":
    FaultSpec(
        fault_id="f3",
        service="payment-service",
        base_url=PAYMENT_BASE_URL,
        description="Forced payment authorisation failures (HTTP 500)",
    ),
    "f4":
    FaultSpec(
        fault_id="f4",
        service="inventory-service",
        base_url=INVENTORY_BASE_URL,
        description="Tomcat thread pool exhaustion",
    ),
    "f5":
    FaultSpec(
        fault_id="f5",
        service="inventory-service",
        base_url=INVENTORY_BASE_URL,
        description="Slow heap memory leak",
    ),
    "f6":
    FaultSpec(
        fault_id="f6",
        service="inventory-service",
        base_url=INVENTORY_BASE_URL,
        description="Off-heap spike → Kubernetes OOMKill",
    ),
}

# Ground truth registry


@dataclass(frozen=True)
class GroundTruth:
    """
    The correct submit_diagnosis values for a fault.
    Both component and fault_type are scored independently in artifact.py.
    Values must exactly match the Literal types in common_tools.py:submit_diagnosis.
    """
    service: str
    component: str
    fault_type: str


GROUND_TRUTH: dict[str, GroundTruth] = {
    # F1 — HikariCP holds all DB connections, requests queue indefinitely.
    "f1":
    GroundTruth(
        service="inventory-service",
        component="hikari-connection-pool",
        fault_type="connection-pool-starvation",
    ),
    # F2 — CPU worker threads peg all cores.
    # component="cpu" matches the Literal in submit_diagnosis exactly.
    "f2":
    GroundTruth(
        service="inventory-service",
        component="cpu",
        fault_type="cpu-saturation",
    ),
    # F3 — payment-service returns forced 500s. order-service's Resilience4j
    # CB trips to OPEN. The observable component is in order-service.
    "f3":
    GroundTruth(
        service="order-service",
        component="resilience4j-circuit-breaker",
        fault_type="circuit-breaker-open",
    ),
    # F4 — Tomcat worker threads blocked in lock.wait() inside the fault trap.
    "f4":
    GroundTruth(
        service="inventory-service",
        component="tomcat-thread-pool",
        fault_type="thread-pool-exhaustion",
    ),
    # F5 — Heap accumulates past GC recovery. 210s fixed wait, both conditions.
    "f5":
    GroundTruth(
        service="inventory-service",
        component="jvm-heap",
        fault_type="memory-leak",
    ),
    # F6 — Off-heap exceeds pod 850Mi limit. K8s OOMKills the container.
    "f6":
    GroundTruth(
        service="inventory-service",
        component="kubernetes-pod",
        fault_type="pod-oomkill",
    ),
}

# Convenience accessors

VALID_FAULT_IDS: frozenset[str] = frozenset(FAULT_CATALOGUE.keys())


def get_fault(fault_id: str) -> FaultSpec:
    """Return FaultSpec for fault_id. Raises KeyError on unknown id."""
    if fault_id not in FAULT_CATALOGUE:
        raise KeyError(
            f"Unknown fault_id '{fault_id}'. Valid: {sorted(VALID_FAULT_IDS)}")
    return FAULT_CATALOGUE[fault_id]


def get_ground_truth(fault_id: str) -> GroundTruth:
    """Return GroundTruth for fault_id. Raises KeyError on unknown id."""
    if fault_id not in GROUND_TRUTH:
        raise KeyError(
            f"No ground truth registered for fault_id '{fault_id}'.")
    return GROUND_TRUTH[fault_id]


def fault_injection_url(fault_id: str, action: str) -> str:
    """
    Build the fault injection endpoint URL.
    e.g. fault_injection_url("f1", "activate")
      → "http://127.0.0.1:30081/internal/fault/activate/f1"
    """
    spec = get_fault(fault_id)
    return f"{spec.base_url}/internal/fault/{action}/{fault_id}"
