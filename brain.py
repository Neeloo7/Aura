"""Ollama brain: streaming chat, native tool dispatch, sentence chunker."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import ollama

import config
import os_tools

# Rolling window so num_ctx=2048 never blows 8GB VRAM.
_MAX_HISTORY_MESSAGES = 4

TokenCallback = Callable[[str], None]


def _as_dict(message: Any) -> dict:
    if isinstance(message, dict):
        return dict(message)
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    return dict(message)


def _tool_calls_of(message: Any) -> list:
    if isinstance(message, dict):
        return list(message.get("tool_calls") or [])
    return list(getattr(message, "tool_calls", None) or [])


def _call_name_and_args(call: Any) -> tuple[str, dict]:
    payload = call if isinstance(call, dict) else _as_dict(call)
    fn = payload.get("function") or payload
    if not isinstance(fn, dict):
        fn = _as_dict(fn)
    name = fn.get("name") or ""
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return name, args


def iter_sentence_chunks(text: str) -> Iterator[str]:
    """Yield complete clauses on `.`, `!`, `?` for zero-latency TTS."""
    buffer = []
    for char in text:
        buffer.append(char)
        if char in ".!?":
            chunk = "".join(buffer).strip()
            if chunk:
                yield chunk
            buffer = []
    leftover = "".join(buffer).strip()
    if leftover:
        yield leftover


def _build_messages(user_text: str, chat_history: list[dict] | None) -> list[dict]:
    recent = list(chat_history or [])[-_MAX_HISTORY_MESSAGES:]
    messages: list[dict] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
    messages.extend(recent)
    messages.append({"role": "user", "content": user_text})
    return messages


def _chat(messages: list[dict], *, with_tools: bool) -> Any:
    kwargs: dict[str, Any] = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "options": config.OLLAMA_OPTIONS,
    }
    if with_tools:
        kwargs["tools"] = os_tools.OLLAMA_TOOLS_SCHEMA
    return ollama.chat(**kwargs)


def _stream_chat(messages: list[dict], on_token: TokenCallback | None) -> str:
    pieces: list[str] = []
    stream = ollama.chat(
        model=config.OLLAMA_MODEL,
        messages=messages,
        options=config.OLLAMA_OPTIONS,
        stream=True,
    )
    for chunk in stream:
        token = (chunk.get("message") or {}).get("content") or ""
        if not token:
            continue
        pieces.append(token)
        if on_token:
            on_token(token)
    return "".join(pieces)


def _dispatch_tools(assistant_msg: Any) -> list[dict]:
    """Run every tool_call on the assistant message and return tool-role messages."""
    tool_messages: list[dict] = []
    for call in _tool_calls_of(assistant_msg):
        func_name, func_args = _call_name_and_args(call)
        print(f"[⚡ AI-ru executing tool: {func_name} with args {func_args}]")
        result = os_tools.execute_tool(func_name, func_args)
        print(f"[✓ {func_name} → {result}]")
        tool_messages.append(
            {
                "role": "tool",
                "content": str(result),
                "name": func_name,
            }
        )
    return tool_messages


class Brain:
    """Holds rolling chat history and talks to Ollama with OS tool dispatch."""

    def __init__(self) -> None:
        self.history: list[dict] = []

    def reset(self) -> None:
        self.history.clear()

    def process_user_query(
        self,
        user_text: str,
        chat_history: list[dict] | None = None,
        *,
        on_token: TokenCallback | None = None,
    ) -> str:
        history = self.history if chat_history is None else chat_history
        messages = _build_messages(user_text, history)

        try:
            response = _chat(messages, with_tools=True)
        except Exception as exc:
            return (
                "The local model stalled — quit Ollama in the tray and reopen it. "
                f"({exc})"
            )

        assistant_msg = response.get("message") or {}
        messages.append(_as_dict(assistant_msg))

        if _tool_calls_of(assistant_msg):
            messages.extend(_dispatch_tools(assistant_msg))
            try:
                final_text = _stream_chat(messages, on_token)
            except Exception as exc:
                return f"Tool ran, but the follow-up reply failed: {exc}"
        else:
            final_text = (assistant_msg.get("content") or "").strip()
            if on_token and final_text:
                on_token(final_text)

        final_text = (final_text or "").strip()
        if not final_text:
            final_text = "Done."

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": final_text})
        del history[:-_MAX_HISTORY_MESSAGES]
        return final_text


_brain = Brain()


def process_user_query(
    user_text: str,
    chat_history: list[dict] | None = None,
    *,
    on_token: TokenCallback | None = None,
) -> str:
    """Run one user turn: Ollama → optional OS tools → spoken/text reply."""
    return _brain.process_user_query(user_text, chat_history, on_token=on_token)


if __name__ == "__main__":
    print(f"AI-ru brain  |  model={config.OLLAMA_MODEL}  |  type 'quit' to exit")
    print('Try: "Open Notepad and check my system stats"\n')
    session: list[dict] = []
    while True:
        try:
            line = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in {"q", "quit", "exit"}:
            break
        print("AI-ru > ", end="", flush=True)
        streamed = []

        def _echo(token: str, _buf=streamed) -> None:
            _buf.append(token)
            print(token, end="", flush=True)

        reply = process_user_query(line, session, on_token=_echo)
        if not streamed:
            print(reply, end="")
        print("\n")
