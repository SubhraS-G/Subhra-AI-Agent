"""Agentic loop: LLM + tools, iterating until a final answer.

UNDERSTAND -> PLAN -> EXECUTE -> VERIFY -> LEARN, with the LLM doing the
understanding/planning and the tools doing verified execution. The loop is
bounded (max_turns) so a confused model can never spin forever.
"""

from __future__ import annotations

import json

from system.llm.client import LLMClient, LLMError, StreamInterrupted, ToolSpec
from system.llm.system_prompt import build_system_prompt
from system.llm.tools import build_tools, dispatch
from system.logging_utils import log
from system.memory import MemoryStore

DEFAULT_MAX_TURNS = 6


class AgentLoop:
    def __init__(
        self,
        client: LLMClient | None,
        memory: MemoryStore,
        max_turns: int = DEFAULT_MAX_TURNS,
        fallback_clients: list | None = None,
    ) -> None:
        self.clients: list = ([client] if client else []) + list(fallback_clients or [])
        self.memory = memory
        self.max_turns = max_turns
        self.tools: list[ToolSpec] = build_tools(memory)

    def run(self, user_message: str, history: list[dict] | None = None) -> dict:
        """Run one user request through the tool-using loop.

        Tries each configured provider in order (NVIDIA -> Groq -> Gemini -> ...)
        when one fails with a quota/rate-limit/unreachable error.
        """
        messages: list[dict] = [
            {"role": "system", "content": build_system_prompt(self.memory)}
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        trace: list[dict] = []
        for turn in range(1, self.max_turns + 1):
            response = None
            last_error = ""
            for client in self.clients:
                try:
                    response = client.chat(messages, tools=self.tools)
                    break
                except LLMError as exc:
                    last_error = str(exc)
                    log("agent_loop_provider_failed", turn=turn,
                        provider=getattr(client.config, "provider", "?"),
                        error=str(exc)[:200])
                    continue
            if response is None:
                log("agent_loop_llm_error", turn=turn, error=last_error)
                return {
                    "reply": (
                        f"All configured LLM providers failed: {last_error}\n"
                        "The system is still usable in rule-based mode: "
                        "`python -m cli thumbnail ...`, `python -m cli status`."
                    ),
                    "tool_trace": trace,
                }

            if not response.tool_calls:
                return {"reply": response.text.strip(), "tool_trace": trace}

            # Execute every requested tool, feed results back, continue.
            messages.append({
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": response.tool_calls,
            })
            for call in response.tool_calls:
                name = call.get("name", "")
                arguments = call.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                log("tool_call", tool=name, turn=turn)
                result = dispatch(self.tools, name, arguments)
                trace.append({"turn": turn, "tool": name, "arguments": arguments, "result": result})
                # Provider-neutral tool result message; the client adapters
                # translate for providers that need a different shape.
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", name),
                    "name": name,
                    "content": result,
                })

        return {
            "reply": (
                "I hit my tool-use limit before finishing. Partial progress is "
                "logged; ask me to continue and I will pick up from the trace."
            ),
            "tool_trace": trace,
        }

    # ------------------------------------------------------------ streaming

    def _stream_fallback(self, messages: list[dict]):
        """Try each configured provider until one yields events.

        The first event is consumed eagerly so connection failures surface
        here (a generator's body runs on its first next()) — but once any
        text has been yielded, a fallback would duplicate it, so mid-stream
        StreamInterrupted errors propagate to the caller instead.

        Returns (client, event_iter) or raises LLMError.
        """
        errors: list[str] = []
        for candidate in self.clients:
            try:
                events = candidate.chat_stream(messages, tools=self.tools)
                first = next(events, None)
            except LLMError as exc:
                errors.append(
                    f"{getattr(candidate.config, 'provider', '?')}: {exc}")
                log("agent_loop_provider_failed",
                    provider=getattr(candidate.config, "provider", "?"),
                    error=str(exc)[:200])
                continue

            def _replay(head=first, rest=events):
                if head is not None:
                    yield head
                yield from rest

            return candidate, _replay()
        raise LLMError(" | ".join(errors) or "no LLM providers configured")

    def run_stream(self, user_message: str, history: list[dict] | None = None):
        """Streaming twin of run(): yields events instead of returning once.

        Event shapes (a session is exactly one of the two kinds):
          {"type": "text", "text": str}            — reply token(s)
          {"type": "done", "reply": str, "tool_trace": [...]}
          {"type": "error", "reply": str, "tool_trace": [...]}

        Tool-call turns buffer (tools run between model calls), so the UI
        sees tool activity only through the final trace — same as the
        non-streaming path, but every pure-text reply arrives live.
        """
        messages: list[dict] = [
            {"role": "system", "content": build_system_prompt(self.memory)}
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        trace: list[dict] = []
        for turn in range(1, self.max_turns + 1):
            try:
                client, events = self._stream_fallback(messages)
            except LLMError as exc:
                yield {"type": "error", "reply": (
                    f"All configured LLM providers failed: {exc}\n"
                    "The system is still usable in rule-based mode: "
                    "`python -m cli thumbnail ...`, `python -m cli status`."
                ), "tool_trace": trace}
                return

            text_parts: list[str] = []
            tool_calls: list[dict] = []
            interrupted: StreamInterrupted | None = None
            try:
                for event in events:
                    if event["type"] == "text":
                        text_parts.append(event["text"])
                        yield event
                    elif event["type"] == "tool_calls":
                        tool_calls = event["tool_calls"]
                    elif event["type"] == "done":
                        break
            except StreamInterrupted as exc:
                interrupted = exc
            text = "".join(text_parts).strip() or (interrupted.partial_text if interrupted else "")
            if interrupted and not text:
                yield {"type": "error", "reply": f"The reply stream died: {interrupted}", "tool_trace": trace}
                return
            if interrupted:
                break                                # show what arrived; stop the loop

            if not tool_calls:
                yield {"type": "done", "reply": text, "tool_trace": trace}
                return

            # Tool turn: execute, feed back, keep streaming the next call.
            messages.append({
                "role": "assistant",
                "content": text,
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                name = call.get("name", "")
                arguments = call.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                log("tool_call", tool=name, turn=turn)
                result = dispatch(self.tools, name, arguments)
                trace.append({"turn": turn, "tool": name, "arguments": arguments, "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", name),
                    "name": name,
                    "content": result,
                })

        yield {"type": "done", "reply": (
            "I hit my tool-use limit before finishing. Partial progress is "
            "logged; ask me to continue and I will pick up from the trace."
        ), "tool_trace": trace}
