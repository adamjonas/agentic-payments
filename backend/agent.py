"""Agent loop — Claude-powered tool-use loop that yields SSE events."""

import json
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import anthropic

from backend.config import AGENT_MAX_TURNS, AGENT_MODEL
from backend.tools import (
    TOOL_SCHEMAS,
    execute_tool,
    narrate_tool_call,
    narrate_tool_result,
)

SYSTEM_PROMPT = """\
You are a research and shopping agent that can browse the web, access paid \
resources, and buy products — all using Lightning Network micropayments \
(L402 protocol).

**Research tasks:**
1. Search the web to find relevant sources.
2. Fetch URLs to read their content. If a resource requires payment (HTTP 402), \
   the system will automatically pay the Lightning invoice on your behalf.
3. Synthesize what you learned into a clear, helpful answer.

**Shopping tasks:**
1. Use shop_search to find products on Amazon or Walmart.
2. Use shop_quote to get exact pricing (including shipping and fees).
3. ALWAYS present the quote to the user and ask for explicit confirmation \
   before placing an order. Include: product name, price, shipping cost, \
   total, and ask for their shipping address if not provided.
4. Only after the user confirms, use shop_order to place the order. \
   This will pay via Lightning automatically.

**Important rules:**
- NEVER place an order without the user's explicit confirmation of the price \
  and shipping address.
- Be cost-conscious — check your balance if unsure about funds.
- Always cite sources with URLs when presenting research.
- When showing products, format them clearly with name, price, and rating.
- Keep responses concise.\
"""


@dataclass
class SSEEvent:
    """A single event to be sent to the client via SSE."""

    event: str  # "step", "result", "result_delta", "error", "done"
    data: dict[str, Any]


async def run_agent(
    query: str, *, history: list[dict[str, Any]] | None = None
) -> AsyncGenerator[SSEEvent, None]:
    """Run the agent loop, yielding SSE events as the agent works.

    Event types:
    - step:         The agent is doing something (tool call with narration)
    - result_delta: A chunk of the final answer (streamed token-by-token)
    - result:       The complete final answer (sent after all deltas)
    - error:        Something went wrong
    - done:         Stream is finished
    """
    try:
        async for event in _agent_loop(query, history=history):
            yield event
    except Exception as e:
        yield SSEEvent(event="error", data={"text": f"Unexpected error: {e}"})
    finally:
        yield SSEEvent(event="done", data={})


async def _agent_loop(
    query: str, *, history: list[dict[str, Any]] | None = None
) -> AsyncGenerator[SSEEvent, None]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        yield SSEEvent(
            event="error",
            data={
                "text": (
                    "Missing ANTHROPIC_API_KEY. Restart the app with your "
                    "Anthropic API key to use the agent."
                )
            },
        )
        return

    client = anthropic.AsyncAnthropic()

    # Build messages with conversation history for multi-turn support
    messages: list[dict[str, Any]] = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": query})

    yield SSEEvent(event="step", data={"text": "Thinking..."})

    for turn in range(AGENT_MAX_TURNS):
        # Use streaming API for faster time-to-first-token
        try:
            collected = await _stream_response(client, messages)
        except anthropic.APIError as e:
            yield SSEEvent(event="error", data={"text": f"API error: {e}"})
            return
        except Exception as e:
            yield SSEEvent(event="error", data={"text": f"Stream error: {e}"})
            return

        text_parts = collected["text_parts"]
        tool_use_blocks = collected["tool_use_blocks"]
        raw_content = collected["raw_content"]

        # If no tool calls, stream the final answer
        if not tool_use_blocks:
            final_text = "\n".join(text_parts)
            # Stream in chunks for faster perceived response
            for chunk in _chunk_text(final_text, 60):
                yield SSEEvent(event="result_delta", data={"text": chunk})
            yield SSEEvent(event="result", data={"text": final_text})
            return

        # Emit any intermediate text
        for text in text_parts:
            if text.strip():
                yield SSEEvent(
                    event="step", data={"text": text, "type": "thinking"}
                )

        # Add assistant message to conversation
        messages.append({"role": "assistant", "content": raw_content})

        # Execute each tool call
        tool_results: list[dict[str, Any]] = []
        for tool_block in tool_use_blocks:
            tool_name = tool_block["name"]
            tool_args = tool_block["input"]

            narration = narrate_tool_call(tool_name, tool_args)
            yield SSEEvent(
                event="step",
                data={"text": narration, "type": "tool_call", "tool": tool_name},
            )

            result = await execute_tool(tool_name, tool_args)

            if result.narration:
                yield SSEEvent(
                    event="step",
                    data={
                        "text": result.narration,
                        "type": "tool_result",
                        "tool": tool_name,
                    },
                )

            extra = narrate_tool_result(tool_name, result.output)
            if extra and extra != result.narration:
                yield SSEEvent(
                    event="step",
                    data={"text": extra, "type": "tool_result", "tool": tool_name},
                )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_block["id"],
                    "content": json.dumps(result.output),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    yield SSEEvent(
        event="error",
        data={"text": f"Agent reached maximum turns ({AGENT_MAX_TURNS}) without finishing."},
    )


async def _stream_response(
    client: anthropic.AsyncAnthropic,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Call Claude with streaming and collect the full response.

    Returns a dict with:
    - text_parts: list of text strings from text blocks
    - tool_use_blocks: list of dicts with name, id, input
    - raw_content: the raw content blocks for message history
    """
    text_parts: list[str] = []
    tool_use_blocks: list[dict[str, Any]] = []
    raw_content = []

    current_text = ""
    current_tool: dict[str, Any] | None = None
    current_tool_json = ""

    async with client.messages.stream(
        model=AGENT_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=TOOL_SCHEMAS,
        messages=messages,
    ) as stream:
        async for event in stream:
            if event.type == "content_block_start":
                if event.content_block.type == "text":
                    current_text = ""
                elif event.content_block.type == "tool_use":
                    current_tool = {
                        "id": event.content_block.id,
                        "name": event.content_block.name,
                        "input": {},
                    }
                    current_tool_json = ""

            elif event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    current_text += event.delta.text
                elif event.delta.type == "input_json_delta":
                    current_tool_json += event.delta.partial_json

            elif event.type == "content_block_stop":
                if current_text:
                    text_parts.append(current_text)
                    raw_content.append({"type": "text", "text": current_text})
                    current_text = ""
                if current_tool is not None:
                    if current_tool_json:
                        current_tool["input"] = json.loads(current_tool_json)
                    tool_use_blocks.append(current_tool)
                    raw_content.append({
                        "type": "tool_use",
                        "id": current_tool["id"],
                        "name": current_tool["name"],
                        "input": current_tool["input"],
                    })
                    current_tool = None
                    current_tool_json = ""

    return {
        "text_parts": text_parts,
        "tool_use_blocks": tool_use_blocks,
        "raw_content": raw_content,
    }


def _chunk_text(text: str, size: int) -> list[str]:
    """Split text into chunks for streaming to the frontend."""
    if len(text) <= size:
        return [text]
    chunks = []
    i = 0
    while i < len(text):
        end = min(i + size, len(text))
        # Try to break at a space or newline
        if end < len(text):
            brk = text.rfind(" ", i, end)
            nl = text.rfind("\n", i, end)
            best = max(brk, nl)
            if best > i:
                end = best + 1
        chunks.append(text[i:end])
        i = end
    return chunks
