# Central configuration for the diagnostic agent implementation.
# All modules import constants from here.

import os
from dotenv import load_dotenv

load_dotenv()

# Kubernetes cluster configuration

NAMESPACE = "bookstore-testbed"

# Minikube NodePort base URLs — accessible from the host machine.
# These are stable across pod restarts because NodePort assignments are fixed
# in the service manifests.
INVENTORY_BASE_URL = os.getenv("INVENTORY_BASE_URL", "http://127.0.0.1:30081")
ORDER_BASE_URL = os.getenv("ORDER_BASE_URL", "http://127.0.0.1:30082")
PAYMENT_BASE_URL = os.getenv("PAYMENT_BASE_URL", "http://127.0.0.1:30083")

# Map service names (matching K8s label app=<name>) to their base URLs.
# Used by Condition B tools that call Spring Actuator endpoints directly.

SERVICE_BASE_URLS: dict[str, str] = {
    "inventory-service": INVENTORY_BASE_URL,
    "order-service": ORDER_BASE_URL,
    "payment-service": PAYMENT_BASE_URL,
}

# Valid service names — used for input validation in tools.
VALID_SERVICES: frozenset[str] = frozenset(SERVICE_BASE_URLS.keys())

# Pod resource limits — used by get_resource_metrics to compute percentages.
# CPU in millicores, memory in bytes.

_MiB = 1024 * 1024

POD_RESOURCE_LIMITS: dict[str, dict[str, int]] = {
    "inventory-service": {
        "cpu_millicores": 1000,  # limits.cpu: "1.0"
        "memory_bytes": 850 * _MiB,  # limits.memory: "850Mi"
    },
    "order-service": {
        "cpu_millicores": 1000,  # limits.cpu: "1.0"
        "memory_bytes": 700 * _MiB,  # limits.memory: "700Mi"
    },
    "payment-service": {
        "cpu_millicores": 1000,  # limits.cpu: "1.0" — 
        "memory_bytes": 700 * _MiB,  # limits.memory: "700Mi" — 
    },
}

# Log tool configuration

# Number of raw lines fetched from the K8s logs API before level filtering.
# Large fixed buffer — filtering happens in Python after retrieval.
LOG_FETCH_BUFFER = 500

# Maximum number of filtered log lines returned to the agent per tool call.
# Truncation is applied AFTER level filtering, on line count (not characters),
# to avoid splitting JSON log entries mid-line.
MAX_LOG_LINES = 100

# Filtering returns lines at or above the requested level.
LOG_LEVEL_HIERARCHY: list[str] = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR"]

# Default log level when the caller does not specify one.
# WARN is preferred for fault diagnosis — reduces noise from INFO-level.
DEFAULT_LOG_LEVEL = "WARN"

# LLM / LM Studio configuration

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL",
                               "http://localhost:1234/v1")
LM_STUDIO_API_KEY = os.getenv("LM_STUDIO_API_KEY", "lm-studio")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen/qwen3.5-9b_Paritosh")
# MODEL_NAME = os.getenv("MODEL_NAME", "qwen/qwen3.5-9b_Paritosh_with_reasoning"
#                        )  # TEMP OVERRIDE for testing reasoning capabilities
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0.2"))

# Agent session configuration

# Maximum number of ReAct steps (Thought + Action + Observation = 1 step)
# before the session is terminated. Prevents infinite loops.

AGENT_STEP_LIMIT = 20

# HTTP request timeout for all Actuator and fault injection endpoint calls (seconds).
HTTP_TIMEOUT = 10

# Observability conditions

CONDITION_A = "A"
CONDITION_B = "B"
VALID_CONDITIONS: frozenset[str] = frozenset({CONDITION_A, CONDITION_B})

# Actuator HTTP access (Condition B tools)

ACTUATOR_BASE_HOST: str = "127.0.0.1"  # Minikube NodePorts are accessed via localhost from the host machine.

ACTUATOR_NODE_PORTS: dict[str, int] = {
    "inventory-service": 30081,
    "order-service": 30082,
    "payment-service": 30083,
}

ACTUATOR_DEFAULT_TIMEOUT: int = 5  # seconds
