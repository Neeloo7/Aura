"""Ollama brain: streaming chat, native tool dispatch, sentence chunker, and episodic memory."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import ollama

import config
import os_tools

# Rolling window so num_ctx=2048 never blows 8GB VRAM.
_MAX_HISTORY_MESSAGES = 4

TokenCallback = Callable[[str], None]
ThoughtCallback = Callable[[str], None]


class EpisodicMemory:
    """Persistent episodic vector memory store backed by ChromaDB and SQLite in aura_memory."""

    def __init__(self, memory_dir: str | Path | None = None) -> None:
        if memory_dir is None:
            self.memory_dir = Path(__file__).resolve().parent / "aura_memory"
        else:
            self.memory_dir = Path(memory_dir).resolve()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.client = None
        self.collection = None
        self._init_db()

    def _init_db(self) -> None:
        try:
            import chromadb
            self.client = chromadb.PersistentClient(path=str(self.memory_dir))
            self.collection = self.client.get_or_create_collection(
                name="chat_history",
                metadata={"description": "AI-ru persistent episodic memory & user context"},
            )
        except Exception as exc:
            print(f"[⚠️ EpisodicMemory init error: {exc}]")
            self.client = None
            self.collection = None

    def query(self, query_text: str, n_results: int = 3) -> list[str]:
        """Query the local SQLite/ChromaDB memory store for relevant past facts or user context."""
        with self._lock:
            if not self.collection:
                return []
            try:
                count = self.collection.count()
                if count == 0:
                    return []
                k = min(n_results, count)
                results = self.collection.query(
                    query_texts=[query_text],
                    n_results=k,
                )
                documents = results.get("documents") or []
                if documents and documents[0]:
                    retrieved = [doc.strip() for doc in documents[0] if doc and doc.strip()]
                    return retrieved
                return []
            except Exception as exc:
                print(f"[⚠️ EpisodicMemory query error: {exc}]")
                return []

    def store_turn(self, user_text: str, assistant_text: str, metadata: dict | None = None) -> None:
        """Store a conversation turn or fact into episodic memory."""
        with self._lock:
            if not self.collection:
                return
            u_clean = user_text.strip()
            a_clean = assistant_text.strip()
            if not u_clean:
                return
            doc = f"User: {u_clean}\nAI-ru: {a_clean}"
            tid = f"mem_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
            meta = {
                "timestamp": time.time(),
                "datetime": datetime.now().isoformat(),
            }
            if metadata:
                meta.update(metadata)
            try:
                self.collection.add(
                    documents=[doc],
                    ids=[tid],
                    metadatas=[meta],
                )
            except Exception as exc:
                print(f"[⚠️ EpisodicMemory store error: {exc}]")

    def sync_sessions_if_empty(self, sessions_list: list[dict]) -> None:
        """One-time sync of past session chronologies into ChromaDB if empty."""
        with self._lock:
            if not self.collection:
                return
            try:
                if self.collection.count() == 0 and sessions_list:
                    docs, ids, metas = [], [], []
                    for sess in sessions_list:
                        msgs = sess.get("msgs", [])
                        i = 0
                        while i < len(msgs):
                            sender, text = msgs[i]
                            if str(sender).lower() in {"user", "you", "me", "human"}:
                                reply = ""
                                if i + 1 < len(msgs) and str(msgs[i + 1][0]).lower() not in {"user", "you", "me", "human"}:
                                    reply = msgs[i + 1][1]
                                    i += 1
                                doc = f"User: {text.strip()}\nAI-ru: {reply.strip()}"
                                tid = f"hist_{sess.get('id', 0)}_{uuid.uuid4().hex[:8]}"
                                docs.append(doc)
                                ids.append(tid)
                                metas.append({
                                    "timestamp": float(sess.get("ts", time.time())),
                                    "session_id": str(sess.get("id")),
                                })
                            i += 1
                    if docs:
                        self.collection.add(documents=docs, ids=ids, metadatas=metas)
                        print(f"[🧠 EpisodicMemory synchronized {len(docs)} past session items into aura_memory]")
            except Exception as exc:
                print(f"[⚠️ EpisodicMemory sync error: {exc}]")


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


def _build_messages(
    user_text: str,
    chat_history: list[dict] | None,
    memories: list[str] | None = None,
    image_path: str | Path | None = None,
) -> list[dict]:
    system_content = config.SYSTEM_PROMPT
    system_content += (
        "\n\n[STRICT TOOL EXECUTION MANDATE]:\n"
        "If the user asks about what is playing on Spotify/music, what window/app is open, "
        "what is on their screen, or for visual desktop inspection, you MUST call capture_screen_context() IMMEDIATELY. "
        "NEVER ask 'Would you like me to capture your screen?' or ask for permission — execute the tool immediately."
    )
    if memories:
        formatted_memories = "\n".join(f"- {m.strip()}" for m in memories if m.strip())
        system_content += (
            f"\n\n[Retrieved Long-Term Episodic Memory & User Context]:\n"
            f"{formatted_memories}\n"
            f"Use these recalled episodic memories from aura_memory to recognize returning user context, "
            f"maintain continuity across sessions/reboots, and answer personal inquiries naturally."
        )

    recent = list(chat_history or [])[-_MAX_HISTORY_MESSAGES:]
    messages: list[dict] = [{"role": "system", "content": system_content}]
    messages.extend(recent)

    user_msg: dict[str, Any] = {"role": "user", "content": user_text}
    if image_path:
        p = Path(image_path).resolve()
        if p.is_file():
            user_msg["images"] = [str(p)]

    messages.append(user_msg)
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
    """Holds rolling chat history, episodic memory in aura_memory, and talks to Ollama with OS tool dispatch."""

    def __init__(self, memory_dir: str | Path | None = None) -> None:
        self.history: list[dict] = []
        self.memory = EpisodicMemory(memory_dir)

    def reset(self) -> None:
        self.history.clear()

    def process_user_query(
        self,
        user_text: str,
        chat_history: list[dict] | None = None,
        *,
        on_token: TokenCallback | None = None,
        on_thought: ThoughtCallback | None = None,
        image_path: str | Path | None = None,
    ) -> str:
        history = self.history if chat_history is None else chat_history

        # 1. Query episodic memory store for relevant past facts or user context
        if on_thought:
            if image_path:
                on_thought("Analyzing active screen context with vision model...")
            else:
                on_thought("Searching episodic memory in aura_memory...")

        memories = self.memory.query(user_text, n_results=3)
        if memories and on_thought and not image_path:
            on_thought(f"Recalled {len(memories)} relevant past context item(s) from memory.")

        # 2. Inject retrieved memories and screen vision image into messages
        messages = _build_messages(user_text, history, memories=memories, image_path=image_path)

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
            tool_calls = _tool_calls_of(assistant_msg)
            messages.extend(_dispatch_tools(assistant_msg))

            # If tool captured screen context, attach the screenshot to the follow-up request
            screen_captured = any(
                _call_name_and_args(call)[0] in {"capture_screen_context", "screen_context"}
                for call in tool_calls
            )
            if screen_captured:
                cand = Path("screen_context.png")
                if not cand.is_file():
                    cand = Path(tempfile.gettempdir()) / "screen_context.png"
                if cand.is_file():
                    messages.append({
                        "role": "user",
                        "content": f"The active screen was captured successfully into '{cand.name}'. Here is the visual image. Please inspect the image and directly answer my question: '{user_text}'.",
                        "images": [str(cand.resolve())],
                    })

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

        # Keep rolling in-memory window
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": final_text})
        del history[:-_MAX_HISTORY_MESSAGES]

        # 3. Store conversation turn in persistent episodic memory store
        try:
            self.memory.store_turn(user_text, final_text)
        except Exception as exc:
            print(f"[⚠️ Memory store error: {exc}]")

        return final_text


_brain = Brain()


def process_user_query(
    user_text: str,
    chat_history: list[dict] | None = None,
    *,
    on_token: TokenCallback | None = None,
    on_thought: ThoughtCallback | None = None,
    image_path: str | Path | None = None,
) -> str:
    """Run one user turn: Ollama → optional OS tools → spoken/text reply."""
    return _brain.process_user_query(
        user_text,
        chat_history,
        on_token=on_token,
        on_thought=on_thought,
        image_path=image_path,
    )


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
