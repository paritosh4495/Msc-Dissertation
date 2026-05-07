
# AgentState — the single source of truth for a diagnostic session.

from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph.message import add_messages

# Default opening instruction given to the agent at the start of every trial.
# Deliberately vague about which fault — the agent must discover it via tools.
# The harness can override this via the fault_scenario parameter of
# build_initial_state() if needed, but this default is used for all
# standard experiment trials.


_DEFAULT_FAULT_SCENARIO = (
    "Investigate the bookstore microservice testbed. "
    "Check the health and performance of all services. "
    "Identify any faults or anomalies if present. "
    "When you have completed your investigation, call submit_diagnosis "
    "with your findings. If all services are healthy, set no_fault_detected=True."
)

class AgentState(TypedDict):
    """
    State for a single diagnostic session.
    """

    # Full message history for this session.
    # Initialised with [SystemMessage, HumanMessage] by build_initial_state().
    # Subsequent messages alternate AIMessage / ToolMessage pairs.
    # add_messages reducer: appends new messages, updates existing by message ID.
    messages: Annotated[list[BaseMessage], add_messages]

    # Observability condition for this session. "A" or "B".
    # Read by nodes for logging only — tool set is fixed at graph build time.
    condition: str

    # Number of complete ReAct steps executed so far.
    # Incremented by tools_node after each tool execution round.
    # Used by check_termination edge and harness for RQ metrics.
    step_count: int

    # True once submit_diagnosis returns status="success".
    # Checked by check_termination edge to route to END immediately.
    terminated: bool

    # The system prompt used for this session.
    # Set once at init. Written into the trial artefact for full auditability.
    system_prompt: str


def build_initial_state(
    condition: str,
    system_prompt: str,
    fault_scenario: str = _DEFAULT_FAULT_SCENARIO,
) -> AgentState:
    """
    Build the initial AgentState for a diagnostic trial.

    This is the only sanctioned way to construct AgentState.
    Always use this function — never build the dict manually in the harness.
    """
    return AgentState(
        messages=[
            SystemMessage(content=system_prompt),
            HumanMessage(content=fault_scenario),
        ],
        condition=condition,
        step_count=0,
        terminated=False,
        system_prompt=system_prompt,
    )