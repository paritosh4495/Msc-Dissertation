# Tools shared across both observability conditions.

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
from schemas.schemas import DiagnosisSubmission, ToolResponse
from utils.k8s_utils import core_v1_api, get_pod

logger = logging.getLogger(__name__)

# get_application_logs


def get_application_logs(
    service: str,
    last_n_lines: int = 50,
    level: Optional[str] = None,
) -> ToolResponse:
    """
    Returns recent log lines from a service pod filtered by level.
    level options: DEBUG, INFO, WARN, ERROR (default WARN).
    If the pod has restarted, previous container logs are also returned
    under previous_logs — use these to find OOMKill or crash reasons.
    """
    tool_name = "get_application_logs"

    if service not in VALID_SERVICES:
        return ToolResponse(
            tool=tool_name,
            status="error",
            service=service,
            error_message=(f"Unknown service '{service}'. "
                           f"Valid services: {sorted(VALID_SERVICES)}"),
        )

    # --- Resolve and validate level ---
    resolved_level = (level or DEFAULT_LOG_LEVEL).upper().strip()
    if resolved_level not in LOG_LEVEL_HIERARCHY:
        logger.warning(f"[{tool_name}] Unrecognised log level '{level}'. "
                       f"Falling back to '{DEFAULT_LOG_LEVEL}'.")
        resolved_level = DEFAULT_LOG_LEVEL

    min_level_index = LOG_LEVEL_HIERARCHY.index(resolved_level)

    # --- Cap last_n_lines ---
    requested_lines = min(max(1, last_n_lines), MAX_LOG_LINES)

    try:
        pod = get_pod(service, NAMESPACE)
        if pod is None:
            return ToolResponse(
                tool=tool_name,
                status="error",
                service=service,
                error_message=f"No pod found for service '{service}'.",
            )

        pod_name = pod.metadata.name
        restart_count = 0
        if pod.status.container_statuses:
            restart_count = pod.status.container_statuses[0].restart_count or 0

        # --- Current container logs ---
        current = _fetch_and_filter(
            pod_name=pod_name,
            namespace=NAMESPACE,
            min_level_index=min_level_index,
            requested_lines=requested_lines,
            previous=False,
        )

        # --- Previous container logs (only if pod has restarted) ---
        previous: Optional[dict] = None
        if restart_count > 0:
            previous = _fetch_and_filter(
                pod_name=pod_name,
                namespace=NAMESPACE,
                min_level_index=min_level_index,
                requested_lines=requested_lines,
                previous=True,
            )

        return ToolResponse(
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
        return ToolResponse(
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
    Fetch LOG_FETCH_BUFFER raw lines from the K8s Logs API, parse as
    JSON, filter by level, truncate to requested_lines.

    Non-JSON lines (JVM banner, raw stack trace fragments) are included
    as {"raw": "<line>", "level": "UNKNOWN"} so the agent can see them.

    Returns {"lines": list, "truncated": bool}.
    """
    try:
        raw_log = core_v1_api().read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=LOG_FETCH_BUFFER,
            previous=previous,
        )
    except Exception as e:
        # K8s returns 400 Bad Request when previous=True and the pod
        # hasn't restarted, or when the log buffer has been recycled.
        # This is expected — return empty quietly.
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
            line_level = str(entry.get("level", "")).upper()

            if line_level in LOG_LEVEL_HIERARCHY:
                if LOG_LEVEL_HIERARCHY.index(line_level) >= min_level_index:
                    parsed.append(entry)
            else:
                # Level field missing or unrecognised.
                # Include only when filtering at WARN or below to avoid noise.
                if min_level_index <= LOG_LEVEL_HIERARCHY.index("WARN"):
                    parsed.append(entry)

        except (json.JSONDecodeError, ValueError):
            # Non-JSON line — JVM banner, raw exception fragment, etc.
            # Include as raw so the agent is not blind to it.
            parsed.append({"raw": line, "level": "UNKNOWN"})

    # Truncate to requested_lines — keep the most recent (tail of list)
    truncated = len(parsed) > requested_lines
    if truncated:
        parsed = parsed[-requested_lines:]

    return {"lines": parsed, "truncated": truncated}


# submit_diagnosis


def submit_diagnosis(
    service: Literal[
        "inventory-service",
        "order-service",
        "payment-service",
    ],
    component: Literal[
        "hikari-connection-pool",
        "cpu",
        "resilience4j-circuit-breaker",
        "tomcat-thread-pool",
        "jvm-heap",
        "kubernetes-pod",
    ],
    fault_type: Literal[
        "connection-pool-starvation",
        "cpu-saturation",
        "circuit-breaker-open",
        "thread-pool-exhaustion",
        "memory-leak",
        "pod-oomkill",
    ],
    evidence: str,
    no_fault_detected: bool = False,
) -> ToolResponse:
    """
    Submit your final diagnosis. This ends the session.
    Call when you have clear evidence of the root cause, or set
    no_fault_detected=True if all services are confirmed healthy.
    """
    logger.info(f"[submit_diagnosis] service={service} component={component} "
                f"fault_type={fault_type} no_fault={no_fault_detected}")

    # Validate evidence length
    if not evidence or len(evidence.strip()) < 10:
        return ToolResponse(
            tool="submit_diagnosis",
            status="error",
            error="evidence must be at least 10 characters.",
        )

    # Construct the internal record
    submission = DiagnosisSubmission(
        service=service,
        component=component,
        fault_type=fault_type,
        evidence=evidence.strip(),
        no_fault_detected=no_fault_detected,
    )

    return ToolResponse(
        tool="submit_diagnosis",
        status="success",
        service=submission.service,  # top-level service field
        data={
            "submitted": True,
            "no_fault_detected": submission.no_fault_detected,
            "service": submission.service,
            "component": submission.component,
            "fault_type": submission.fault_type,
            "evidence": submission.evidence,
        },
    )


# def submit_diagnosis(
#     service: str,
#     component: str,
#     fault_type: str,
#     evidence: str,
# ) -> ToolResponse:
#     """
#     Submit the agent's final diagnosis. This is the terminal action.
#     """
#     tool_name = "submit_diagnosis"

#     try:
#         submission = DiagnosisSubmission(
#             service=service,
#             component=component,
#             fault_type=fault_type,
#             evidence=evidence,
#         )
#         return ToolResponse(
#             tool=tool_name,
#             status="success",
#             service=submission.service,
#             data={
#                 "submitted": True,
#                 "service": submission.service,
#                 "component": submission.component,
#                 "fault_type": submission.fault_type,
#                 "evidence": submission.evidence,
#             },
#         )

#     except ValidationError as e:
#         # Return errors to the agent — it can reason and resubmit.
#         return ToolResponse(
#             tool=tool_name,
#             status="error",
#             service=service,
#             error_message=f"Validation failed — {e.errors()}",
#         )

#     except Exception as e:
#         logger.exception(f"[{tool_name}] Unexpected error")
#         return ToolResponse(
#             tool=tool_name,
#             status="error",
#             error_message=f"Unexpected error: {e}",
#         )
