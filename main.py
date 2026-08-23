import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
from difflib import SequenceMatcher
from dotenv import load_dotenv
from tools import TOOLS, execute_tool
from skills import handle_skill_command
from prompts import handle_prompt_command

load_dotenv()

CONFIGS = {
    "OpenAI": {
        "api_key_env": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/chat/completions",
        "headers_fn": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    },
    "OpenRouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "headers_fn": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/cli-chatbot",
            "X-Title": "CLI Chatbot"
        }
    },
    "Groq": {
        "api_key_env": "GROQ_API_KEY",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "headers_fn": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
    },
    "Google Gemini (OpenAI Compatible)": {
        "api_key_env": "GOOGLE_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "headers_fn": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    }
}

# List of OpenAI-compatible providers and their model endpoint configurations
PROVIDERS = {
    "OpenAI": {
        "api_key_env": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/models",
        "headers_fn": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    },
    "OpenRouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "url": "https://openrouter.ai/api/v1/models",
        "headers_fn": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    },
    "Groq": {
        "api_key_env": "GROQ_API_KEY",
        "url": "https://api.groq.com/openai/v1/models",
        "headers_fn": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
    },
    "Google Gemini (OpenAI Compatible)": {
        "api_key_env": "GOOGLE_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "headers_fn": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    }
}

def fetch_models(provider_name, config):
    api_key = os.getenv(config["api_key_env"])
    if not api_key:
        return None

    headers = config["headers_fn"](api_key)
    req = urllib.request.Request(config["url"], headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
            
            models_list = []
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    models_list = data["data"]
                elif "models" in data and isinstance(data["models"], list):
                    models_list = data["models"]
            elif isinstance(data, list):
                models_list = data

            clean_models = []
            for m in models_list:
                if isinstance(m, dict):
                    model_id = m.get("id") or m.get("name") or str(m)
                else:
                    model_id = str(m)
                clean_models.append(model_id)
            return clean_models

    except Exception:
        return None

def discover_models():
    all_discovered = {}
    
    for provider_name, config in PROVIDERS.items():
        models = fetch_models(provider_name, config)
        if models is not None:
            all_discovered[provider_name] = models

    output_json = json.dumps(all_discovered, indent=2)
    print(output_json)
    
    with open("discovered_models.json", "w", encoding="utf-8") as f:
        f.write(output_json)

def load_models():
    if not os.path.exists("discovered_models.json"):
        print("Error: discovered_models.json not found. Please run discover_models.py first.")
        sys.exit(1)
    with open("discovered_models.json", "r", encoding="utf-8") as f:
        return json.load(f)

def search_models(all_models, query=""):
    flat_list = []
    for provider, models in all_models.items():
        for m in models:
            flat_list.append((provider, m))
    
    if not query:
        return flat_list

    query_lower = query.lower()
    scored_results = []
    
    for provider, model in flat_list:
        combined_str = f"{provider} {model}".lower()
        if query_lower in combined_str:
            score = 1.0 + (len(query_lower) / len(combined_str))
        else:
            score = SequenceMatcher(None, query_lower, combined_str).ratio()
            
        if score > 0.3:
            scored_results.append((score, provider, model))
            
    scored_results.sort(key=lambda x: x[0], reverse=True)
    return [(provider, model) for score, provider, model in scored_results]

def send_chat_request(provider, model_id, messages, tools=None):
    config = CONFIGS.get(provider)
    if not config:
        return {"error": f"Error: Unknown provider '{provider}'"}
    
    api_key = os.getenv(config["api_key_env"])
    if not api_key:
        return {"error": f"Error: API key for {provider} ({config['api_key_env']}) is not set in environment."}

    payload = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    
    req_data = json.dumps(payload).encode("utf-8")
    print("\n")
    print("req_data", json.dumps(payload, indent=2))
    print("\n")
    headers = config["headers_fn"](api_key)
    req = urllib.request.Request(config["url"], data=req_data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            print("\n")
            print("res_json", json.dumps(res_json, indent=2))
            print("\n")
            message = res_json["choices"][0]["message"]
            return {"message": message}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        return {"error": f"HTTP Error {e.code}: {error_body}"}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

def select_model(all_models):
    """Prompt for a model. Returns (provider, model) or None if the user quits."""
    while True:
        print("\n--- Model Selection ---")
        search_query = input("Search models (type keyword, press Enter for all, or 'q' to quit): ").strip()
        if search_query.lower() == 'q':
            return None

        matches = search_models(all_models, search_query)
        if not matches:
            print("No models found matching your query. Try again.")
            continue

        print(f"\nFound {len(matches)} matching models (showing up to 30):")
        display_list = matches[:30]
        for idx, (prov, mod) in enumerate(display_list, 1):
            print(f"  {idx:2d}. [{prov}] {mod}")

        if len(matches) > 30:
            print(f"  ... and {len(matches) - 30} more. Refine your search if needed.")

        choice = input("\nSelect a model number (or press Enter to search again): ").strip()
        if not choice:
            continue
        try:
            choice_idx = int(choice)
            if 1 <= choice_idx <= len(display_list):
                return display_list[choice_idx - 1]
            else:
                print("Invalid number selection. Try again.")
        except ValueError:
            print("Invalid input. Please enter a valid number.")

SHELL_TIMEOUT = 60
SHELL_CONTEXT_LIMIT = 4000

def run_shell(command):
    """Run a shell command locally and return its combined output as text."""
    cmd = command.strip()
    if not cmd:
        return "[no command given]"

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"[timed out after {SHELL_TIMEOUT}s]"
    except Exception as e:
        return f"[shell error] {e}"

    output = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
    if proc.returncode != 0:
        output = f"{output}\n[exit {proc.returncode}]" if output else f"[exit {proc.returncode}]"
    return output or "[no output]"

def chat_loop(selected_provider, selected_model):
    """Run the chat session. Returns 'switch' to pick a new model, or 'exit'."""
    print(f"\n=> Selected Model: [{selected_provider}] {selected_model}")
    print("Tools enabled: get_current_time, calculate, read_file_tool")
    print("Type your message below. Commands: '/exit' or '/quit' to exit, '/switch' to change model, '/clear' to clear history.")
    print("Shell: '!cmd' runs locally and adds the output to context, '!!cmd' runs locally and keeps it out of context.\n")
    print("Skills: '/skills' lists them, '/skill <name> [task]' loads one, '/skills reload' re-scans.")
    print("Prompts: '/prompts' lists them, '/prompt <name> [task]' loads one, '/prompts reload' re-scans.")

    messages = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            return "exit"

        if not user_input:
            continue

        if user_input.lower() in ["/exit", "/quit"]:
            print("Goodbye!")
            return "exit"
        if user_input.startswith("/skill"):
            expanded = handle_skill_command(user_input)
            if expanded is None:
                continue
            user_input = expanded
        elif user_input.startswith("/prompt"):
            expanded = handle_prompt_command(user_input)
            if expanded is None:
                continue
            user_input = expanded
        elif user_input.lower() == "/clear":
            messages = []
            print("[Conversation history cleared]\n")
            continue
        elif user_input.lower() == "/switch":
            return "switch"

        # '!!cmd' -> run locally, print output, keep it out of the conversation.
        # '!cmd'  -> run locally, print output, record it so the model sees it next turn.
        if user_input.startswith("!"):
            silent = user_input.startswith("!!")
            cmd = user_input[2:].strip() if silent else user_input[1:].strip()
            output = run_shell(cmd)
            print(f"{output}\n")

            if not silent:
                recorded = output
                if len(recorded) > SHELL_CONTEXT_LIMIT:
                    recorded = recorded[:SHELL_CONTEXT_LIMIT] + "\n[...output truncated]"
                messages.append({
                    "role": "user",
                    "content": f"[shell] $ {cmd}\n{recorded}"
                })
            continue

        messages.append({"role": "user", "content": user_input})

        while True:
            print(f"{selected_model} is thinking...", end="\r")
            result = send_chat_request(selected_provider, selected_model, messages, tools=TOOLS)
            print(" " * 50, end="\r")

            if "error" in result:
                print(f"{result['error']}\n")
                break

            msg = result["message"]

            tool_calls = msg.get("tool_calls")
            if tool_calls:
                messages.append(msg)
                print(f"{selected_model}: [Calling tools...]")

                for tc in tool_calls:
                    call_id = tc["id"]
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        fn_args = {}

                    tool_output = execute_tool(fn_name, fn_args)
                    print(f"[Tool Output for {fn_name}]: {tool_output}")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": tool_output
                    })
                continue
            else:
                reply = msg.get("content") or ""
                print(f"{selected_model}: {reply}\n")
                messages.append({"role": "assistant", "content": reply})
                break

def main():
    print("========================================")
    print("      AI MODEL CLI CHATBOT + TOOLS      ")
    print("========================================")

    print("\n")
    print(json.dumps(TOOLS, indent=2))
    print(type(TOOLS))
    print("\n")

    discover_models()

    all_models = load_models()

    while True:
        selection = select_model(all_models)
        if selection is None:
            return

        selected_provider, selected_model = selection
        if chat_loop(selected_provider, selected_model) != "switch":
            return

if __name__ == "__main__":
    main()