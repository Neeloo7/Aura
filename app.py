import streamlit as st
import ollama
import subprocess
import psutil
import chromadb
import urllib.parse
import threading
import time
import os
import asyncio
import edge_tts
import pygame
import pyautogui

# --- 1. Neural Voice Engine ---
def speak_text(text: str):
    def run_speech():
        clean_text = text.replace('*', '').replace('#', '')
        async def generate_and_play():
            try:
                communicate = edge_tts.Communicate(clean_text, "en-US-AriaNeural")
                await communicate.save("response.mp3")
                pygame.mixer.init()
                pygame.mixer.music.load("response.mp3")
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.1)
                pygame.mixer.music.unload()
                pygame.mixer.quit()
                os.remove("response.mp3")
            except Exception as e:
                print(f"Voice Error: {e}")
        asyncio.run(generate_and_play())
    threading.Thread(target=run_speech).start()

# --- 2. Local Memory (ChromaDB) ---
chroma_client = chromadb.PersistentClient(path="./aura_memory")
collection = chroma_client.get_or_create_collection(name="chat_history")

def store_memory(user_text: str, ai_text: str):
    doc_id = f"msg_{len(collection.get()['ids']) + 1}"
    collection.add(documents=[f"User: {user_text} | Aura: {ai_text}"], ids=[doc_id])

def retrieve_memory(query: str) -> str:
    if len(collection.get()["ids"]) == 0: return ""
    results = collection.query(query_texts=[query], n_results=2)
    return "\n".join(results["documents"][0]) if results["documents"] and results["documents"][0] else ""

# --- 3. Agentic Tools ---
def launch_application(app_name: str) -> str:
    app = app_name.lower().strip()
    local_app_data = os.environ.get('LOCALAPPDATA', '')
    discord_path = os.path.join(local_app_data, "Discord", "Update.exe")
    
    aliases = {
        "file explorer": "explorer", 
        "settings": "start ms-settings:", 
        "task manager": "taskmgr", 
        "discord": f'start "" "{discord_path}" --processStart Discord.exe'
    }
    command = aliases.get(app, f"start {app}")
    try:
        subprocess.Popen(command, shell=True)
        return f"Success: Launched '{app}'."
    except Exception as e: return f"Failed to launch: {str(e)}"

def close_application(app_name: str) -> str:
    app = app_name.lower().strip()
    if not app.endswith(".exe"): app += ".exe"
    try:
        subprocess.run(["taskkill", "/F", "/T", "/IM", app], capture_output=True)
        return f"Success: Closed {app}."
    except Exception as e: return f"Error closing: {str(e)}"

def get_system_metrics() -> str:
    return f"CPU: {psutil.cpu_percent(interval=0.5)}%, RAM: {psutil.virtual_memory().percent}%"

def control_spotify(action: str, query: str = "") -> str:
    action = action.lower()
    try:
        if action == "search":
            subprocess.Popen(f"start spotify:search:{urllib.parse.quote(query)}", shell=True)
            return f"Searched Spotify for '{query}'."
        elif action in ["play", "pause"]:
            pyautogui.press("playpause")
            return "Toggled playback."
    except Exception as e: return f"Spotify error: {str(e)}"
    
def search_web(query: str) -> str:
    try:
        subprocess.Popen(f"start https://www.google.com/search?q={urllib.parse.quote(query)}", shell=True)
        return f"Web search opened for '{query}'."
    except Exception as e: return str(e)

def type_text(text: str) -> str:
    try:
        time.sleep(2)
        pyautogui.write(text, interval=0.03)
        return "Typed text successfully."
    except Exception as e: return str(e)

def navigate_discord(target: str) -> str:
    try:
        pyautogui.hotkey('ctrl', 'k')
        time.sleep(0.5)
        pyautogui.write(target, interval=0.05)
        time.sleep(0.5)
        pyautogui.press('enter')
        return f"Navigated Discord to '{target}'."
    except Exception as e: return str(e)

TOOL_MAP = {
    "launch_application": launch_application, "close_application": close_application, 
    "get_system_metrics": get_system_metrics, "control_spotify": control_spotify,
    "search_web": search_web, "type_text": type_text, "navigate_discord": navigate_discord
}

TOOLS = [
    {"type": "function", "function": {"name": "launch_application", "description": "Opens a Windows app.", "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]}}},
    {"type": "function", "function": {"name": "close_application", "description": "Closes a Windows app.", "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]}}},
    {"type": "function", "function": {"name": "get_system_metrics", "description": "Checks CPU and RAM.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "control_spotify", "description": "Controls Spotify.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["play", "pause", "search"]}, "query": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "search_web", "description": "Searches Google.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "type_text", "description": "Types text.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "navigate_discord", "description": "Navigates Discord via Quick Switcher.", "parameters": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}}}
]

# --- 4. UI & Persona Setup ---
ALIAS = "Aura"
MODEL = "qwen2.5:7b" 

st.set_page_config(page_title=f"{ALIAS} Workspace", layout="centered")
st.title(f"{ALIAS}: AI Workspace Agent")
voice_enabled = st.sidebar.toggle("Enable Voice Output", value=True)

SYSTEM_PROMPT = f"""You are {ALIAS}, an AI agent on a Windows 11 machine. 
CRITICAL: You MUST use your tools when asked to control the OS, check metrics, play music, search the web, type text, or navigate Discord. 
NEVER assume an application is already open based on past memory. Always execute the tool. Keep answers sharp and concise."""

if "full_chat_history" not in st.session_state:
    st.session_state.full_chat_history = []

for msg in st.session_state.full_chat_history:
    if msg["role"] not in ["system", "tool"]:
        with st.chat_message(msg["role"]): st.markdown(msg["content"])

# --- 5. Main Execution Loop with Rolling VRAM Window ---
if user_input := st.chat_input("Command Aura (or press Win+H to speak)..."):
    with st.chat_message("user"): st.markdown(user_input)
    st.session_state.full_chat_history.append({"role": "user", "content": user_input})

    # VRAM FIX: Only send the last 4 messages to the LLM to prevent the 500 status code crash
    recent_history = st.session_state.full_chat_history[-4:] if len(st.session_state.full_chat_history) > 4 else st.session_state.full_chat_history.copy()
    
    messages_for_llm = [{"role": "system", "content": SYSTEM_PROMPT}]
    past_context = retrieve_memory(user_input)
    if past_context: 
        messages_for_llm.append({"role": "system", "content": f"Memory: {past_context}"})
    
    messages_for_llm.extend(recent_history)

    try:
        response = ollama.chat(model=MODEL, messages=messages_for_llm, tools=TOOLS)
        response_msg = response["message"]
        st.session_state.full_chat_history.append(response_msg)
        messages_for_llm.append(response_msg)

        if response_msg.get("tool_calls"):
            for call in response_msg["tool_calls"]:
                func_name, args = call["function"]["name"], call["function"]["arguments"]
                result = TOOL_MAP[func_name](**args) if func_name in TOOL_MAP else "Tool not found."
                
                tool_result_msg = {"role": "tool", "content": str(result)}
                st.session_state.full_chat_history.append(tool_result_msg)
                messages_for_llm.append(tool_result_msg)
                
            final_response = ollama.chat(model=MODEL, messages=messages_for_llm)
            final_reply = final_response["message"]["content"]
            st.session_state.full_chat_history.append(final_response["message"])
        else:
            final_reply = response_msg["content"]
            
        with st.chat_message("assistant"):
            st.markdown(final_reply)
            if voice_enabled: speak_text(final_reply)
                
        store_memory(user_input, final_reply)

    except Exception as e:
        st.error(f"⚠️ GPU Overload Detected: The background runner stalled. Please right-click Ollama in your system tray, click 'Quit', and reopen it. ({str(e)})")