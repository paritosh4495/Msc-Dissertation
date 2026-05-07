# Condition boundary enforcer and system prompt builder.
#
# get_tools_and_prompt(condition) is the single entry point.
# It returns the exact tool set for the condition — no tool from the
# wrong condition is ever registered — and the condition-specific
# system prompt.

import logging
from typing import Optional
from langchain_core.tools import StructuredTool
from config import VALID_CONDITIONS
from tools.common_tools import get_application_logs, submit_diagnosis
from tools.condition_a_tools import (
    get_pod_events,
    get_resource_metrics,
    get_service_health_a,
)
from tools.condition_b_tools import (
    get_circuit_breaker_state,
    get_service_health_b,
    query_actuator_metrics,
)

logger = logging.getLogger(__name__)

# Internal helpers


def _wrap(fn) -> StructuredTool:
    """
    Wrap a plain Python diagnostic tool function as a LangChain StructuredTool.
    """
    return StructuredTool.from_function(fn)




def get_tools_and_prompt(condition: str) -> tuple[list[StructuredTool], str]:
    """
    Return the tool set and system prompt for the given condition.

    This is the condition boundary enforcer. Only tools registered here
    for a condition are available to the agent. No tool from the wrong
    condition can leak through.
    """
    if condition not in VALID_CONDITIONS:
        raise ValueError(f"Invalid condition '{condition}'. "
                         f"Must be one of: {sorted(VALID_CONDITIONS)}")

    if condition == "A":
        tools = [
            _wrap(get_service_health_a),
            _wrap(get_resource_metrics),
            _wrap(get_application_logs),
            _wrap(get_pod_events),
            _wrap(submit_diagnosis),
        ]
        prompt = _build_system_prompt("A")

    else:  # condition == "B"
        tools = [
            _wrap(get_service_health_b),
            _wrap(query_actuator_metrics),
            _wrap(get_circuit_breaker_state),
            _wrap(get_application_logs),
            _wrap(submit_diagnosis),
        ]
        prompt = _build_system_prompt("B")

    logger.info(f"Registered {len(tools)} tools for condition {condition}: "
                f"{[t.name for t in tools]}")

    return tools, prompt
