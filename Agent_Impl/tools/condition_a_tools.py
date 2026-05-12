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
from utils.k8s_utils import (
    core_v1_api,
    custom_api,
    get_pod,
    get_pod_name,
    parse_cpu_to_millicores,
    parse_memory_to_bytes,
)
from utils.tool_utils import make_tool_response

logger = logging.getLogger(__name__)


def get_service_health_a(service: str) -> dict:
    """
    Returns the Kubernetes-level health of a service pod, including its phase, 
    readiness status, and restart count. 
    Use this as a starting point to check if a pod is crashing (non-zero restart count) 
    or failing its readiness probe. 
    A non-zero restart_count warrants calling get_pod_events even if the service is currently UP.
    A status of 'DEGRADED' or 'DOWN' often points to a service-level failure.
    """
    tool_name = "get_service_health_a"

    if service not in VALID_SERVICES:
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=
            f"Unknown service '{service}'. Valid services: {sorted(VALID_SERVICES)}",
        )

    try:
        pod = get_pod(service, NAMESPACE)

        if pod is None:
            return make_tool_response(
                tool=tool_name,
                status="error",
                service=service,
                error_message=
                f"No pod found for service '{service}' in namespace '{NAMESPACE}'.",
            )

        # Extract pod phase
        pod_phase: str = pod.status.phase or "Unknown"

        # Check Readiness condition
        ready: bool = False
        if pod.status.conditions:
            for condition in pod.status.conditions:
                if condition.type == "Ready":
                    ready = condition.status == "True"
                    break

        # Extract restart count from the first container
        restart_count: int = 0
        if pod.status.container_statuses:
            restart_count = pod.status.container_statuses[0].restart_count or 0

        # Summarize status
        if pod_phase == "Running" and ready:
            status_summary = "UP"
        elif pod_phase == "Running" and not ready:
            status_summary = "DEGRADED"
        else:
            status_summary = "DOWN"

        return make_tool_response(
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
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=f"Unexpected error: {str(e)}",
        )


def get_resource_metrics(service: str) -> dict:
    """
    Retrieves real-time CPU and memory usage for a service pod as percentages 
    of its configured Kubernetes limits. 
    Use this to identify resource-related faults like CPU saturation or high 
    memory pressure. 
    Note that metrics are snapshots and may have a slight lag from the Metrics Server.
    """
    tool_name = "get_resource_metrics"

    if service not in VALID_SERVICES:
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=
            f"Unknown service '{service}'. Valid services: {sorted(VALID_SERVICES)}",
        )

    try:
        pod_name = get_pod_name(service, NAMESPACE)
        if pod_name is None:
            return make_tool_response(
                tool=tool_name,
                status="error",
                service=service,
                error_message=f"No pod found for service '{service}'.",
            )

        # Query the K8s Metrics API
        result = custom_api().get_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=NAMESPACE,
            plural="pods",
            name=pod_name,
        )

        containers = result.get("containers", [])
        if not containers:
            return make_tool_response(
                tool=tool_name,
                status="error",
                service=service,
                error_message="Metrics Server returned no container data.",
            )

        usage = containers[0]["usage"]
        cpu_raw = usage.get("cpu", "0m")
        memory_raw = usage.get("memory", "0Ki")

        # Canonicalize units
        cpu_millicores = parse_cpu_to_millicores(cpu_raw)
        memory_bytes = parse_memory_to_bytes(memory_raw)

        # Retrieve configured limits
        limits = POD_RESOURCE_LIMITS[service]
        cpu_limit_mc = limits["cpu_millicores"]
        memory_limit_b = limits["memory_bytes"]

        # Compute usage percentages
        cpu_pct = round((cpu_millicores / cpu_limit_mc) *
                        100, 2) if cpu_limit_mc > 0 else 0.0
        memory_pct = round((memory_bytes / memory_limit_b) *
                           100, 2) if memory_limit_b > 0 else 0.0

        return make_tool_response(
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
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=f"Unexpected error: {str(e)}",
        )


def get_pod_events(service: str) -> dict:
    """
    Fetches recent Kubernetes events related to the service pod, such as 
    restarts, OOMKills, or scheduling failures. 
    Warning events are prioritized at the top of the list. 
    Call this whenever get_service_health_a reports a non-zero restart_count,
    regardless of current status. 
    Also use this when a service is DOWN or DEGRADED to find the underlying
    infrastructure cause.

    """
    tool_name = "get_pod_events"

    if service not in VALID_SERVICES:
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=
            f"Unknown service '{service}'. Valid services: {sorted(VALID_SERVICES)}",
        )

    try:
        events = core_v1_api().list_namespaced_event(namespace=NAMESPACE, )

        # Filter for pod-related events for this service
        relevant = [
            e for e in events.items if e.involved_object.kind == "Pod"
            and e.involved_object.name.startswith(service)
        ]

        if not relevant:
            return make_tool_response(
                tool=tool_name,
                status="success",
                service=service,
                data={
                    "event_count": 0,
                    "events": [],
                    "note": f"No pod events found for service '{service}'.",
                },
            )

        # Sort: Warning first, then by timestamp descending
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

        return make_tool_response(
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
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=f"Unexpected error: {str(e)}",
        )
