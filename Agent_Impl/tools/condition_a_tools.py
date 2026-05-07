# Condition A diagnostic tools — Generic External Observability.

import json
import logging
from typing import Optional

from config import (
    NAMESPACE,
    POD_RESOURCE_LIMITS,
    LOG_FETCH_BUFFER,
    MAX_LOG_LINES,
    LOG_LEVEL_HIERARCHY,
    DEFAULT_LOG_LEVEL,
    VALID_SERVICES,
)
from schemas.schemas import ToolResponse
from utils.k8s_utils import (
    core_v1_api,
    custom_api,
    get_pod,
    get_pod_name,
    parse_cpu_to_millicores,
    parse_memory_to_bytes,
)

logger = logging.getLogger(__name__)

# Tool 1 — get_service_health_a


def get_service_health_a(service: str) -> ToolResponse:
    """
    Returns Kubernetes pod health: phase, readiness, restart count,
    and a status_summary of UP / DEGRADED / DOWN.
    This is a point-in-time snapshot of pod state at the moment of the call.
    Use this first to identify which service is degraded.
    """
    tool_name = "get_service_health_a"

    if service not in VALID_SERVICES:
        return ToolResponse(
            tool=tool_name,
            status="error",
            service=service,
            error_message=
            f"Unknown service '{service}'. Valid services: {sorted(VALID_SERVICES)}",
        )

    try:
        pod = get_pod(service, NAMESPACE)

        if pod is None:
            return ToolResponse(
                tool=tool_name,
                status="error",
                service=service,
                error_message=
                f"No pod found for service '{service}' in namespace '{NAMESPACE}'.",
            )

        # --- Extract phase ---
        pod_phase: str = pod.status.phase or "Unknown"

        # --- Extract Ready condition ---
        # pod.status.conditions is a list of V1PodCondition objects.
        # The "status" field is the string "True" or "False" — not a Python bool.
        ready: bool = False
        if pod.status.conditions:
            for condition in pod.status.conditions:
                if condition.type == "Ready":
                    ready = condition.status == "True"
                    break

        # --- Extract restart count ---
        restart_count: int = 0
        if pod.status.container_statuses:
            restart_count = pod.status.container_statuses[0].restart_count or 0

        # --- Derive status summary ---
        if pod_phase == "Running" and ready:
            status_summary = "UP"
        elif pod_phase == "Running" and not ready:
            status_summary = "DEGRADED"
        else:
            status_summary = "DOWN"

        return ToolResponse(
            tool=tool_name,
            status="success",
            service=service,
            data={
                "status_summary": status_summary,
                "pod_phase": pod_phase,
                "ready": ready,
                "restart_count": restart_count,
                "pod_name": pod.metadata.name,
            },
        )

    except Exception as e:
        logger.exception(
            f"[{tool_name}] Unexpected error for service '{service}'")
        return ToolResponse(
            tool=tool_name,
            status="error",
            service=service,
            error_message=f"Unexpected error: {str(e)}",
        )


# Tool 2 — get_resource_metrics


def get_resource_metrics(service: str) -> ToolResponse:
    """
    Returns current CPU and memory usage as percentages of configured
    pod limits. Use to confirm CPU saturation or memory pressure.
    Point-in-time snapshot — call repeatedly to observe trends.
    """
    tool_name = "get_resource_metrics"

    if service not in VALID_SERVICES:
        return ToolResponse(
            tool=tool_name,
            status="error",
            service=service,
            error_message=
            f"Unknown service '{service}'. Valid services: {sorted(VALID_SERVICES)}",
        )

    try:
        pod_name = get_pod_name(service, NAMESPACE)
        if pod_name is None:
            return ToolResponse(
                tool=tool_name,
                status="error",
                service=service,
                error_message=f"No pod found for service '{service}'.",
            )

        # --- Fetch metrics from Metrics Server ---
        result = custom_api().get_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=NAMESPACE,
            plural="pods",
            name=pod_name,
        )

        containers = result.get("containers", [])
        if not containers:
            return ToolResponse(
                tool=tool_name,
                status="error",
                service=service,
                error_message="Metrics Server returned no container data.",
            )

        usage = containers[0]["usage"]
        cpu_raw = usage.get("cpu", "0m")
        memory_raw = usage.get("memory", "0Ki")

        # --- Parse to canonical units ---
        cpu_millicores = parse_cpu_to_millicores(cpu_raw)
        memory_bytes = parse_memory_to_bytes(memory_raw)

        # --- Retrieve limits from config (sourced from manifests) ---
        limits = POD_RESOURCE_LIMITS[service]
        cpu_limit_mc = limits["cpu_millicores"]
        memory_limit_b = limits["memory_bytes"]

        # --- Compute percentages, guard against division by zero ---
        cpu_pct = round((cpu_millicores / cpu_limit_mc) *
                        100, 2) if cpu_limit_mc > 0 else 0.0
        memory_pct = round((memory_bytes / memory_limit_b) *
                           100, 2) if memory_limit_b > 0 else 0.0

        return ToolResponse(
            tool=tool_name,
            status="success",
            service=service,
            data={
                "cpu_percent":
                cpu_pct,
                "memory_percent":
                memory_pct,
                "cpu_millicores_used":
                cpu_millicores,
                "memory_bytes_used":
                memory_bytes,
                "cpu_limit_millicores":
                cpu_limit_mc,
                "memory_limit_bytes":
                memory_limit_b,
                "pod_name":
                pod_name,
                "note":
                ("Point-in-time snapshot with ~15s Metrics Server scrape lag. "
                 "Call repeatedly to observe trends."),
            },
        )

    except Exception as e:
        logger.exception(
            f"[{tool_name}] Unexpected error for service '{service}'")
        return ToolResponse(
            tool=tool_name,
            status="error",
            service=service,
            error_message=f"Unexpected error: {str(e)}",
        )


# Tool 3 — get_pod_events


def get_pod_events(service: str) -> ToolResponse:
    """
    Returns recent Kubernetes events for the service pod, sorted with
    Warning events first. Use to detect OOMKill, restarts, and
    scheduling failures.
    
    """
    tool_name = "get_pod_events"

    if service not in VALID_SERVICES:
        return ToolResponse(
            tool=tool_name,
            status="error",
            service=service,
            error_message=
            f"Unknown service '{service}'. Valid services: {sorted(VALID_SERVICES)}",
        )

    try:
        events = core_v1_api().list_namespaced_event(namespace=NAMESPACE, )

        # Filter to Pod-kind events whose name starts with the service name.
        # This captures both current and recently terminated pods.
        relevant = [
            e for e in events.items if e.involved_object.kind == "Pod"
            and e.involved_object.name.startswith(service)
        ]

        if not relevant:
            return ToolResponse(
                tool=tool_name,
                status="success",
                service=service,
                data={
                    "event_count": 0,
                    "events": [],
                    "note": f"No pod events found for service '{service}'.",
                },
            )

        # Sort: Warning events first, then Normal; within each group most recent first.
        def _sort_key(e):
            type_rank = 0 if e.type == "Warning" else 1
            ts = e.last_timestamp or e.event_time
            return (type_rank, -(ts.timestamp() if ts else 0))

        relevant.sort(key=_sort_key)

        serialised = []
        for e in relevant:
            last_ts = e.last_timestamp or e.event_time
            serialised.append({
                "type":
                e.type,
                "reason":
                e.reason,
                "message":
                e.message,
                "count":
                e.count,
                "last_seen":
                last_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if last_ts else None,
                "pod_name":
                e.involved_object.name,
            })

        return ToolResponse(
            tool=tool_name,
            status="success",
            service=service,
            data={
                "event_count": len(serialised),
                "events": serialised,
            },
        )

    except Exception as e:
        logger.exception(
            f"[{tool_name}] Unexpected error for service '{service}'")
        return ToolResponse(
            tool=tool_name,
            status="error",
            service=service,
            error_message=f"Unexpected error: {str(e)}",
        )
