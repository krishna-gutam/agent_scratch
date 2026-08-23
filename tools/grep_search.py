import os
import re

from .decorator import tool

@tool("""Search for a pattern in files within the workspace.

    Args:
        pattern: The regex pattern to search for.
        path: The directory or file to search in. Defaults to '.'.
    """)
def grep_search(pattern: str, path: str = ".") -> str:
    """Search for a pattern in files within the workspace."""
    root = os.path.abspath(os.getcwd())
    target = os.path.abspath(os.path.join(root, path))
    
    if not target.startswith(root):
        return f"Error: {path} is outside the workspace."

    if not os.path.exists(target):
        return f"Error: {path} does not exist."

    results = []
    count = 0
    max_matches = 50

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"

    if os.path.isfile(target):
        files_to_search = [target]
    else:
        files_to_search = []
        for dirpath, _, filenames in os.walk(target):
            for f in filenames:
                files_to_search.append(os.path.join(dirpath, f))

    for file_path in files_to_search:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if regex.search(line):
                        rel_path = os.path.relpath(file_path, root)
                        results.append(f"{rel_path}:{i}:{line.strip()}")
                        count += 1
                        if count >= max_matches:
                            break
        except Exception as e:
            continue
        if count >= max_matches:
            break

    if not results:
        return "No matches found."
    
    output = "\n".join(results)
    if count >= max_matches:
        output += "\n\n[Truncated to 50 matches]"
    
    return output
