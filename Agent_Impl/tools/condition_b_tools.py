# Condition B diagnostic tools — Framework-Native Observability via Spring Actuator.

from __future__ import annotations

import logging
from typing import Optional

from config import VALID_SERVICES
from utils.actuator_utils import actuator_get
from utils.tool_utils import make_tool_response

logger = logging.getLogger(__name__)

# Limits for circuit breaker event processing
_CB_MAX_EVENTS = 20
_CB_SUCCESS_KEEP = 5


def get_service_health_b(service: str) -> dict:
    """
    Retrieves the detailed Spring Boot Actuator health report for a service.
    This includes statuses for internal components like the database connection pool (Hikari), 
    disk space, and custom health indicators. 
    Use this to pinpoint which specific subsystem is failing within a service.
    """
    tool_name = "get_service_health_b"

    if service not in VALID_SERVICES:
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=(f"Unknown service '{service}'. "
                           f"Valid services: {sorted(VALID_SERVICES)}"),
        )

    try:
        data = actuator_get(service, "/actuator/health")
        return make_tool_response(
            tool=tool_name,
            status="success",
            service=service,
            data=data,
        )

    except Exception as e:
        logger.exception(f"[{tool_name}] Error for service '{service}'")
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=str(e),
        )


def query_actuator_metrics(
    service: str,
    metric_name: Optional[str] = None,
) -> dict:
    """
    Queries real-time application metrics from Spring Boot Actuator.
    If 'metric_name' is omitted, it returns a list of all available metric names.
    If 'metric_name' is provided, it returns the current value and available tags for that metric.
    
    Commonly useful metrics:
    - hikaricp.connections.active/pending/timeout: For identifying connection pool saturation.
    - jvm.memory.used / jvm.threads.live: For memory leaks or thread exhaustion.
    - process.cpu.usage: For identifying high CPU load.
    - resilience4j.circuitbreaker.state: For checking the current circuit breaker state.
    """
    tool_name = "query_actuator_metrics"

    if service not in VALID_SERVICES:
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=(f"Unknown service '{service}'. "
                           f"Valid services: {sorted(VALID_SERVICES)}"),
        )

    try:
        if metric_name is None:
            # List all available metrics
            raw = actuator_get(service, "/actuator/metrics")
            names = raw.get("names", [])
            return make_tool_response(
                tool=tool_name,
                status="success",
                service=service,
                data={
                    "metric_name":
                    None,
                    "available_metrics":
                    sorted(names),
                    "count":
                    len(names),
                    "note":
                    "Call again with a specific metric_name to get its value.",
                },
            )

        else:
            # Fetch details for the requested metric
            path = f"/actuator/metrics/{metric_name.strip()}"
            raw = actuator_get(service, path)

            return make_tool_response(
                tool=tool_name,
                status="success",
                service=service,
                data={
                    "metric_name": raw.get("name"),
                    "description": raw.get("description"),
                    "base_unit": raw.get("baseUnit"),
                    "measurements": raw.get("measurements", []),
                    "available_tags": raw.get("availableTags", []),
                },
            )

    except Exception as e:
        logger.exception(
            f"[{tool_name}] Error for service '{service}', metric '{metric_name}'"
        )
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=str(e),
        )


def get_circuit_breaker_state(service: str) -> dict:
    """
    Retrieves the current state of Resilience4j circuit breakers for a service, 
    including failure rates and recent event history (state transitions, errors).
    Use this to diagnose issues where a service is returning 503 errors or blocking 
    calls to downstream dependencies.
    """
    tool_name = "get_circuit_breaker_state"

    if service not in VALID_SERVICES:
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=(f"Unknown service '{service}'. "
                           f"Valid services: {sorted(VALID_SERVICES)}"),
        )

    # Fetch current state snapshot
    try:
        state_raw = actuator_get(service, "/actuator/circuitbreakers")
    except Exception as e:
        logger.exception(
            f"[{tool_name}] Failed to fetch circuit breaker state for '{service}'"
        )
        return make_tool_response(
            tool=tool_name,
            status="error",
            service=service,
            error_message=f"Could not fetch circuit breaker state: {e}",
        )

    # Fetch recent events (state transitions and errors)
    events_raw: dict = {}
    events_error: Optional[str] = None
    try:
        events_raw = actuator_get(
            service,
            "/actuator/circuitbreakerevents",
            timeout=8,
        )
    except Exception as e:
        logger.warning(
            f"[{tool_name}] Could not fetch circuit breaker events for '{service}': {e}"
        )
        events_error = str(e)

    all_events = events_raw.get("circuitBreakerEvents", [])
    filtered = _filter_cb_events(all_events)

    cb_states = state_raw.get("circuitBreakers", {})

    return make_tool_response(
        tool=tool_name,
        status="success",
        service=service,
        data={
            "circuit_breakers":
            cb_states,
            "event_count_total":
            len(all_events),
            "events_shown":
            len(filtered),
            "events":
            filtered,
            "events_note":
            "Prioritizes STATE_TRANSITION and ERROR events. Most recent first.",
            **({
                "events_error": events_error
            } if events_error else {}),
        },
    )


def _filter_cb_events(events: list[dict]) -> list[dict]:
    """
    Internal helper to filter and rank circuit breaker events for the agent.
    Prioritizes transitions and errors to surface critical failures.
    """
    # Reverse so most recent is first
    events = list(reversed(events))

    priority = []
    successes = []

    for e in events:
        t = e.get("type", "")
        if t in ("STATE_TRANSITION", "ERROR", "NOT_PERMITTED"):
            priority.append(e)
        elif t == "SUCCESS":
            if len(successes) < _CB_SUCCESS_KEEP:
                successes.append(e)

    combined = priority + successes
    return combined[:_CB_MAX_EVENTS]
