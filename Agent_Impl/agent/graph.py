# Factory for constructing the LangGraph StateGraph used in diagnostic sessions.
# A fresh graph is built per condition and reused across trials.

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
from tools.tool_registry import get_tools
from prompts.system_prompt import build_system_prompt

logger = logging.getLogger(__name__)

# Node names for graph orchestration
_NODE_AGENT = "agent"
_NODE_TOOLS = "tools"

# Each ReAct step involves one agent node and one tools node execution.
# The limit is set to ensure the agent has a final turn to process the last tool output.
GRAPH_RECURSION_LIMIT: int = AGENT_STEP_LIMIT * 2 + 1

def build_agent(condition: str) -> tuple[CompiledStateGraph, str]:
    """
    Builds and compiles a StateGraph for the specified observability condition.
    The graph itself is stateless; trial data is managed via AgentState passed at invocation.
    """
    if condition not in VALID_CONDITIONS:
        raise ValueError(f"Invalid condition '{condition}'. "
                         f"Must be one of: {sorted(VALID_CONDITIONS)}")

    logger.info(f"Building agent for condition {condition}.")

    tools = get_tools(condition)
    system_prompt = build_system_prompt(condition)
    tool_names = [t.name for t in tools]
    logger.info(f"Condition {condition} tools: {tool_names}")

    # LLM configuration via LM Studio
    model = ChatOpenAI(
        base_url=LM_STUDIO_BASE_URL,
        api_key=LM_STUDIO_API_KEY,
        model=MODEL_NAME,
        temperature=MODEL_TEMPERATURE,
    )

    # Bind tools to the model for tool-calling capabilities
    model_with_tools = model.bind_tools(tools)

    # Node initialisation
    agent_node = make_agent_node(model_with_tools)
    tools_node = make_tools_node(tools)

    def should_continue(state: AgentState) -> str:
        """
        Determines the next node based on whether the agent requested tool calls.
        If no tool calls are present, the session ends.
        """
        last_message = state["messages"][-1]
        if last_message.tool_calls:
            return _NODE_TOOLS
        logger.warning(f"[graph] agent produced no tool calls at step "
                       f"{state['step_count']}. Routing to END.")
        return END

    def check_termination(state: AgentState) -> str:
        """
        Checks for session termination conditions: successful diagnosis or step limit reached.
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

    # Graph construction and edge definition
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
