
# build_agent(condition) — factory that constructs and returns a
# compiled LangGraph StateGraph for a diagnostic session.
#
# One compiled graph is built per condition per experiment run.
# The same compiled graph is reused across all trials for that condition.
# Each trial gets a fresh initial state via build_initial_state().



import logging
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.nodes import make_agent_node, make_tools_node
from agent.state import AgentState
from config import (
    AGENT_STEP_LIMIT,
    CONDITION_A,
    CONDITION_B,
    LM_STUDIO_API_KEY,
    LM_STUDIO_BASE_URL,
    MODEL_NAME,
    MODEL_TEMPERATURE,
    VALID_CONDITIONS,
)
from tools.tool_registry import get_tools_and_prompt

logger = logging.getLogger(__name__)

# Node name constants
# Used in add_node / add_edge / add_conditional_edges calls.
# String literals are only written once — here.

_NODE_AGENT = "agent"
_NODE_TOOLS = "tools"

# Recursion limit helper
#
# LangGraph's recursion_limit counts node executions, not ReAct steps.
# Each ReAct step = 1 agent_node execution + 1 tools_node execution = 2.
# The +1 gives the agent_node one final execution to see the last
# ToolMessage and produce its AIMessage (which may have no tool_calls,
# routing to END via should_continue).
#
# Pass this as config={"recursion_limit": GRAPH_RECURSION_LIMIT}
# when calling graph.invoke() in the harness.

GRAPH_RECURSION_LIMIT: int = AGENT_STEP_LIMIT * 2 + 1

# build_agent


def build_agent(condition: str) -> CompiledStateGraph:
    """
    Build and return a compiled LangGraph StateGraph for the given
    observability condition.

    The returned graph is stateless — it holds no session data.
    Invoke it with a fresh AgentState from build_initial_state() for
    each trial.
    """
    if condition not in VALID_CONDITIONS:
        raise ValueError(f"Invalid condition '{condition}'. "
                         f"Must be one of: {sorted(VALID_CONDITIONS)}")

    logger.info(f"Building agent for condition {condition}.")

    # --- Tools and system prompt ---
    tools, system_prompt = get_tools_and_prompt(condition)
    tool_names = [t.name for t in tools]
    logger.info(f"Condition {condition} tools: {tool_names}")

    # --- LLM ---
    # Instantiated here (not at module level) so LM Studio connection
    # is only attempted when build_agent() is called, not at import time.
    model = ChatOpenAI(
        base_url=LM_STUDIO_BASE_URL,
        api_key=LM_STUDIO_API_KEY,
        model=MODEL_NAME,
        temperature=MODEL_TEMPERATURE,
    )

    # Bind tools to model — injects tool schemas into every API call.
    # The LLM uses these schemas to decide when and how to call tools.
    model_with_tools = model.bind_tools(tools, parallel_tool_calls=False)

    # --- Node functions ---
    agent_node = make_agent_node(model_with_tools)
    tools_node = make_tools_node(tools)

    # --- Conditional edge functions ---

    def should_continue(state: AgentState) -> str:
        """
        Route after agent_node.

        If the LLM produced tool calls → execute them.
        If the LLM produced plain text (no tool calls) → end session.
        The harness will detect terminated=False as a NO_SUBMISSION outcome.
        """
        last_message = state["messages"][-1]
        if last_message.tool_calls:
            return _NODE_TOOLS
        logger.warning(f"[graph] agent produced no tool calls at step "
                       f"{state['step_count']}. Routing to END.")
        return END


    def check_termination(state: AgentState) -> str:
        """
        Route after tools_node.

        Terminate if submit_diagnosis succeeded or step limit reached.
        Otherwise continue to next ReAct step.
        """
        if state["terminated"]:
            logger.info(
                f"[graph] Condition {condition} — session terminated "
                f"by successful submit_diagnosis at step {state['step_count']}."
            )
            return END

        if state["step_count"] >= AGENT_STEP_LIMIT:
            logger.warning(f"[graph] Condition {condition} — step limit "
                           f"({AGENT_STEP_LIMIT}) reached. Routing to END.")
            return END

        return _NODE_AGENT

    # --- Build graph ---
    graph_builder = StateGraph(AgentState)

    graph_builder.add_node(_NODE_AGENT, agent_node)
    graph_builder.add_node(_NODE_TOOLS, tools_node)

    graph_builder.add_edge(START, _NODE_AGENT)

    graph_builder.add_conditional_edges(
        _NODE_AGENT,
        should_continue,
        {
            _NODE_TOOLS: _NODE_TOOLS,
            END: END
        },
    )

    graph_builder.add_conditional_edges(
        _NODE_TOOLS,
        check_termination,
        {
            _NODE_AGENT: _NODE_AGENT,
            END: END
        },
    )

    compiled = graph_builder.compile()

    logger.info(
        f"Agent graph for condition {condition} compiled successfully.")

    return compiled, system_prompt
