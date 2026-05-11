# Node definitions for the diagnostic agent graph.
# These functions create the logic for the LLM interaction and tool execution steps.

import json
import logging
from typing import Callable

from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.prebuilt import ToolNode

from agent.state import AgentState

logger = logging.getLogger(__name__)

def make_agent_node(model_with_tools: Runnable) -> Callable:
    """
    Creates the agent node that interacts with the LLM. 
    This node sends the current message history to the model and captures 
    its response. The resulting AIMessage will contain either a text response 
    or specific tool calls that determine the agent's next action.
    """

    def agent_node(state: AgentState) -> dict:
        logger.debug(f"[agent_node] condition={state['condition']} "
                     f"step={state['step_count']} "
                     f"messages={len(state['messages'])}")

        response: AIMessage = model_with_tools.invoke(state["messages"])

        logger.debug(
            f"[agent_node] response tool_calls={len(response.tool_calls)} "
            f"content_length={len(str(response.content))}")

        # Return only the messages key — add_messages reducer appends response.
        return {"messages": [response]}

    return agent_node


def make_tools_node(tools: list[BaseTool]) -> Callable:
    """
    Creates the node responsible for executing tools requested by the agent.
    It runs tool calls in parallel using LangGraph's ToolNode, increments 
    the session step counter, and checks if a successful 'submit_diagnosis' 
    was performed to signal that the session should terminate.
    """
    _tool_node = ToolNode(tools)

    def tools_node(state: AgentState) -> dict:
        logger.debug(f"[tools_node] condition={state['condition']} "
                     f"step={state['step_count']} — executing tool calls")

        # Execute all tool calls from the last message
        tool_result: dict = _tool_node.invoke(state)
        new_tool_messages = tool_result.get("messages", [])

        # Check if the agent submitted a valid diagnosis
        last_ai_message: AIMessage = state["messages"][-1]
        call_id_to_name: dict[str, str] = {
            tc["id"]: tc["name"]
            for tc in last_ai_message.tool_calls
        }

        terminated = state["terminated"]  # carry forward if already True

        for tool_msg in new_tool_messages:
            tool_name = call_id_to_name.get(tool_msg.tool_call_id, "")

            if tool_name == "submit_diagnosis":
                # Mark session as terminated if the diagnosis was accepted
                terminated = _check_submission_succeeded(tool_msg.content)
                if terminated:
                    logger.info(f"[tools_node] submit_diagnosis succeeded — "
                                f"session will terminate after this step.")

        new_step_count = state["step_count"] + 1

        logger.debug(f"[tools_node] step_count now {new_step_count} "
                     f"terminated={terminated}")

        return {
            "messages": new_tool_messages,
            "step_count": new_step_count,
            "terminated": terminated,
        }

    return tools_node


def _check_submission_succeeded(content) -> bool:
    """
    Parses the response from the 'submit_diagnosis' tool to verify success.
    It handles JSON stringified content and checks for the 'submitted' flag.
    """
    # Normalize different ToolMessage content formats
    if isinstance(content, list):
        content = " ".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content)

    # Handle direct dict responses
    if isinstance(content, dict):
        return content.get("data", {}).get("submitted") is True

    if not isinstance(content, str):
        return False

    # Primary path: parse JSON string from ToolNode
    try:
        parsed = json.loads(content)
        return parsed.get("data", {}).get("submitted") is True
    except (json.JSONDecodeError, AttributeError, TypeError):
        logger.warning(
            f"[tools_node] Could not parse submit_diagnosis response: "
            f"{str(content)[:200]}")
        return False
