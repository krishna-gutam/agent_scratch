import os
from .decorator import tool

@tool("""Read up to 1000 lines from a file.

    Args:
        path: Path to the file, relative to the workspace root.
        start_line: The line number to start reading from (1-indexed). Defaults to 1.
    """)
def read_file(path: str, start_line: int = 1) -> str:
    """Read up to 1000 lines from a file.

    Args:
        path: Path to the file, relative to the workspace root.
        start_line: The line number to start reading from (1-indexed). Defaults to 1.
    """
    root = os.path.abspath(os.getcwd())
    full = os.path.abspath(os.path.join(root, path))
    
    if not full.startswith(root + os.sep) and full != root:
        return f"Error: {path} is outside the workspace."

    if not os.path.isfile(full):
        return f"Error: {path} does not exist."

    if start_line < 1:
        return "Error: start_line must be 1 or greater."

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            
        total_lines = len(lines)
        if start_line > total_lines:
             return f"Error: start_line {start_line} is beyond the end of the file (total: {total_lines} lines)."
        
        end_line = start_line + 1000
        requested_lines = lines[start_line - 1 : end_line - 1]
        
        result = "".join(requested_lines)
        
        if end_line - 1 < total_lines:
            result += f"\n\n--- Truncated: read lines {start_line} to {end_line - 1} of {total_lines}. Call with start_line={end_line} to continue. ---"
        else:
            result += f"\n\n--- Read lines {start_line} to {total_lines} of {total_lines}. ---"
            
        return result
    except OSError as e:
        return f"Error reading {path}: {e}"
