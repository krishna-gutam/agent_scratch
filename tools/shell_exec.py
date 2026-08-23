import os
import subprocess
import sys
from .decorator import tool

@tool(    """Executes a PowerShell command.

    Args:
        script: The PowerShell command or script to execute.
        justification: Explain why this command needs to be executed.
    """)
def run_powershell(script: str, justification: str) -> str:
    """Executes a PowerShell command.

    Args:
        script: The PowerShell command or script to execute.
        justification: Explain why this command needs to be executed.
    """

    current_workspace = os.getcwd()
    venv_dir = os.path.join(current_workspace, ".venv")
    venv_win = os.path.join(venv_dir, "Scripts")
    venv_unix = os.path.join(venv_dir, "bin")

    # 1. Create the .venv if it doesn't exist
    if not os.path.exists(venv_dir):
        try:
            subprocess.run([sys.executable, "-m", "venv", ".venv"], check=True)
        except subprocess.CalledProcessError as e:
            return f"Error: Failed to automatically create a '.venv'.\n{e}"

    # 2. Determine OS paths and explicitly locate the executables
    if os.path.exists(venv_win):
        active_venv = venv_win
        python_exe = os.path.join(venv_win, "python.exe")
        pip_exe = os.path.join(venv_win, "pip.exe")
    elif os.path.exists(venv_unix):
        active_venv = venv_unix
        python_exe = os.path.join(venv_unix, "python")
        pip_exe = os.path.join(venv_unix, "pip")
    else:
        return "Error: Virtual environment created, but binary folders are missing."

    # 3. ZERO-FALLBACK ENFORCEMENT:
    # Set strict aliases for python and pip. If they fail, the script hard-crashes.
    injected_script = (
        f"$env:Path = '{active_venv};' + $env:Path; "
        f"Set-Alias -Name python -Value '{python_exe}'; "
        f"Set-Alias -Name pip -Value '{pip_exe}'; "
        f"{script}"
    )

    try:
        result = subprocess.run(
            ["powershell", "-Command", injected_script],
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
        # Return stdout if present, otherwise return stderr, or a clear message
        output = result.stdout.strip()
        error = result.stderr.strip()
        
        # Truncate output to prevent token overflow
        if len(output) > 2000:
            output = output[:2000] + "\n... (truncated)"
        if len(error) > 2000:
            error = error[:2000] + "\n... (truncated)"

        if output:
            return output
        if error:
            return f"Error: {error}"
        return "Command executed successfully (no output)."

    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 60 seconds. It may be stuck in an infinite loop."
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() or e.stdout.strip()
        return f"Error: {error_msg}"
