# Tuneable operational parameters for the experiment harness.
# Change values here for dry runs, subset runs, or timing adjustments.

from __future__ import annotations

# Experiment scope

# Full experiment: 10. Dry run / smoke: 1.
REPETITIONS: int = 1

# Subset of faults to run. Change to e.g. ["f1", "f3"] for partial runs.
# Full experiment uses all six.
FAULT_IDS: list[str] = ["f1", "f2", "f3", "f4", "f5", "f6"]

# Output paths

# Root directory for all trial artefacts and the progress file.
# Relative to Agent_Impl/ (the working directory when the harness is run).
OUTPUT_DIR: str = "trial_results"

# Kubernetes reset + readiness

# Seconds to sleep between kubectl delete and kubectl apply during hard reset.
# Gives the node time to release ports and CPU before reapply.
K8S_SLEEP_BETWEEN_DELETE_AND_APPLY_S: int = 15

# Timeout for each `kubectl rollout status deployment/<svc>` call.
POD_READY_TIMEOUT_S: int = 180

# Timeout for `kubectl wait --for=delete pod --all` during teardown.
POD_DELETE_TIMEOUT_S: int = 120

# Timeout for postgres readiness wait (app services depend on it).
POSTGRES_READY_TIMEOUT_S: int = 60

# All deployments the harness waits on after kubectl apply.
# Order matters: postgres first (app services depend on it),
# then the three app services, then load-generator.
SERVICES_TO_WAIT: list[str] = [
    "inventory-service",
    "order-service",
    "payment-service",
    "load-generator",
]

# Fixed JVM warmup pause after every k8s_hard_reset, before baseline check.
JVM_WARMUP_WAIT_S: int = 30

# Baseline check

# Maximum time to spend polling for a clean baseline after warmup.
# If not satisfied within this window, trial is marked EXCLUDED.
BASELINE_TIMEOUT_S: int = 60

# Poll interval during baseline check.
BASELINE_POLL_INTERVAL_S: int = 5

# Fault materialisation — per fault

# F1: HikariCP starvation. Fixed wait — load-generator provides steady traffic.
F1_MATERIALISE_WAIT_S: int = 30

# F2: CPU saturation. Fixed wait — worker threads ramp up within seconds.
F2_MATERIALISE_WAIT_S: int = 30

# F3: Circuit breaker. Binary poll — wait until CB state == OPEN.
F3_CB_POLL_INTERVAL_S: int = 5
F3_CB_POLL_TIMEOUT_S: int = 60

# F4: Thread pool exhaustion. Fixed wait after load workers are running.
# Load workers start before fault activation; 90s gives the pool time to
# saturate fully before the agent begins observing.
F4_MATERIALISE_WAIT_S: int = 90
F4_LOAD_CONCURRENCY: int = 60  # concurrent threads from harness machine

# F5: Heap leak. Symmetric fixed wait for BOTH conditions.
# 210s chosen to ensure heap accumulates past GC recovery threshold (~60%)
# for the agent to see a clear signal regardless of observability condition.
F5_MATERIALISE_WAIT_S: int = 210

# F6: OOMKill. Event-driven poll — wait until restart_count > 0.
F6_POLL_INTERVAL_S: int = 3
F6_POLL_TIMEOUT_S: int = 120

# Token cost estimation (RQ4)

# Cost per 1,000,000 tokens in USD. (OpenRouter Estimate as of May 2026)
PRICE_PER_1M_INPUT_TOKENS: float = 0.04
PRICE_PER_1M_OUTPUT_TOKENS: float = 0.15
