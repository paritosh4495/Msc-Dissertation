# Diagnostic tools shared across both observability conditions.

import json
import logging
from typing import Optional, Literal

from pydantic import ValidationError

from config import (
    NAMESPACE,
    LOG_FETCH_BUFFER,
    MAX_LOG_LINES,
    LOG_LEVEL_HIERARCHY,
    DEFAULT_LOG_LEVEL,
    VALID_SERVICES,
)
from utils.tool_utils import make_tool_response
from utils.k8s_utils import core_v1_api, get_pod

logger = logging.getLogger(__name__)


def get_application_logs(
    service: str,
    level: Optional[str] = None,
) -> dict:
    """
    Retrieves recent application logs for a specified service, filtered by severity level.
    Use this to inspect runtime errors, exceptions, or unexpected application behavior.
    Supported levels: DEBUG, INFO, WARN, ERROR (defaults to WARN).
    If the service pod has restarted, logs from the previous container instance are 
    included in 'previous_logs', which is critical for diagnosing OOMKills or fatal crashes.
    """
    tool_name = "get_application_logs"

    if service not in VALID_SERVICES:
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=(f"Unknown service '{service}'. "
                           f"Valid services: {sorted(VALID_SERVICES)}"),
        )

    # Resolve and validate level; fallback to DEFAULT_LOG_LEVEL if unknown
    resolved_level = (level or DEFAULT_LOG_LEVEL).upper().strip()
    if resolved_level not in LOG_LEVEL_HIERARCHY:
        logger.warning(f"[{tool_name}] Unrecognised log level '{level}'. "
                       f"Falling back to '{DEFAULT_LOG_LEVEL}'.")
        resolved_level = DEFAULT_LOG_LEVEL

    min_level_index = LOG_LEVEL_HIERARCHY.index(resolved_level)
    requested_lines = MAX_LOG_LINES

    try:
        pod = get_pod(service, NAMESPACE)
        if pod is None:
            return make_tool_response(
                tool=tool_name,
                status="error",
                service=service,
                error_message=f"No pod found for service '{service}'.",
            )

        pod_name = pod.metadata.name
        restart_count = 0
        if pod.status.container_statuses:
            restart_count = pod.status.container_statuses[0].restart_count or 0

        # Fetch current logs
        current = _fetch_and_filter(
            pod_name=pod_name,
            namespace=NAMESPACE,
            min_level_index=min_level_index,
            requested_lines=requested_lines,
            previous=False,
        )

        # Fetch previous logs if the pod has restarted
        previous: Optional[dict] = None
        if restart_count > 0:
            previous = _fetch_and_filter(
                pod_name=pod_name,
                namespace=NAMESPACE,
                min_level_index=min_level_index,
                requested_lines=requested_lines,
                previous=True,
            )

        return make_tool_response(
            tool=tool_name,
            status="success",
            service=service,
            data={
                "level_filter": resolved_level,
                "pod_name": pod_name,
                "pod_restarted": restart_count > 0,
                "restart_count": restart_count,
                "current_logs": current["lines"],
                "current_truncated": current["truncated"],
                "previous_logs": previous["lines"] if previous else None,
                "previous_truncated":
                previous["truncated"] if previous else None,
            },
        )

    except Exception as e:
        logger.exception(
            f"[{tool_name}] Unexpected error for service '{service}'")
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=f"Unexpected error: {e}",
        )


def _fetch_and_filter(
    pod_name: str,
    namespace: str,
    min_level_index: int,
    requested_lines: int,
    previous: bool,
) -> dict:
    """
    Internal helper to fetch, parse, and filter K8s logs.
    Handles ECS-formatted structured logs and falls back to 'level' key.
    Raw lines (banners, stack trace fragments) are included as 'UNKNOWN' to avoid data loss.
    """
    try:
        raw_log = core_v1_api().read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=LOG_FETCH_BUFFER,
            previous=previous,
        )
    except Exception as e:
        logger.debug(
            f"Could not fetch {'previous' if previous else 'current'} logs "
            f"for pod '{pod_name}': {e}")
        return {"lines": [], "truncated": False}

    if not raw_log:
        return {"lines": [], "truncated": False}

    parsed: list[dict] = []

    for raw_line in raw_log.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        try:
            entry = json.loads(line)

            # Extract log level from ECS dotted key or standard 'level' key
            line_level = str(entry.get("log.level")
                             or entry.get("level", "")).upper()

            if line_level in LOG_LEVEL_HIERARCHY:
                if LOG_LEVEL_HIERARCHY.index(line_level) >= min_level_index:
                    parsed.append(entry)
            else:
                # Include unrecognised levels for lower thresholds to capture potential issues
                if min_level_index <= LOG_LEVEL_HIERARCHY.index("WARN"):
                    parsed.append(entry)

        except (json.JSONDecodeError, ValueError):
            # Include non-JSON content as raw lines to ensure visibility of banners/exceptions
            parsed.append({"raw": line, "level": "UNKNOWN"})

    # Truncate to the most recent lines
    truncated = len(parsed) > requested_lines
    if truncated:
        parsed = parsed[-requested_lines:]

    return {"lines": parsed, "truncated": truncated}


def submit_diagnosis(
    evidence: str,
    no_fault_detected: bool = False,
    service: Optional[Literal["inventory-service", "order-service",
                              "payment-service"]] = None,
    component: Optional[Literal["hikari-connection-pool", "cpu",
                                "resilience4j-circuit-breaker",
                                "tomcat-thread-pool", "jvm-heap",
                                "kubernetes-pod"]] = None,
    fault_type: Optional[Literal["connection-pool-starvation",
                                 "cpu-saturation", "circuit-breaker-open",
                                 "thread-pool-exhaustion", "memory-leak",
                                 "pod-oomkill"]] = None,
) -> dict:
    """
    Submits your final root cause diagnosis and terminates the diagnostic session.
    Provide a detailed 'evidence' string explaining your findings and the specific data 
    points (logs, metrics) that support your conclusion.
    If your investigation confirms all services are healthy and no fault is active, 
    set 'no_fault_detected' to True.
    This tool MUST be called to conclude the task once the root cause is identified.
    """
    logger.info(f"[submit_diagnosis] service={service} component={component} "
                f"fault_type={fault_type} no_fault={no_fault_detected}")

    # Ensure evidence is substantial enough
    if not evidence or len(evidence.strip()) < 10:
        return make_tool_response(
            tool="submit_diagnosis",
            status="error",
            error_message="evidence must be at least 10 characters.",
        )

    return make_tool_response(
        tool="submit_diagnosis",
        status="success",
        service=service,
        data={
            "submitted": True,
            "no_fault_detected": no_fault_detected,
            "service": service,
            "component": component,
            "fault_type": fault_type,
            "evidence": evidence.strip(),
        },
    )
