import functools

_DECORATED_TOOLS = {}


def tool(description=None):
    """Register a Python function as an AI-callable tool."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper.tool_name = func.__name__
        wrapper.tool_description = description or func.__doc__ or ""
        _DECORATED_TOOLS[func.__name__] = wrapper
        return wrapper

    return decorator
