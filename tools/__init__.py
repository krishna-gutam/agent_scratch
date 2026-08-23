import importlib
import inspect
import json
import pkgutil
from typing import Union, get_args, get_origin

from .decorator import _DECORATED_TOOLS

TOOLS = []
TOOL_REGISTRY = {}


def _python_type_to_json_schema(annotation) -> str:
    if annotation is inspect.Parameter.empty:
        return "string"

    origin = get_origin(annotation)
    if origin is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if args:
            annotation = args[0]

    mapping = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    return mapping.get(annotation, "string")


def _build_parameter_schema(func) -> dict:
    sig = inspect.signature(func)
    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        param_type = _python_type_to_json_schema(param.annotation)
        prop_spec = {"type": param_type}

        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        else:
            if param.default is not None:
                prop_spec["default"] = param.default

        properties[param_name] = prop_spec

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def load_tools():
    global TOOLS, TOOL_REGISTRY
    TOOLS = []
    TOOL_REGISTRY = {}
    _DECORATED_TOOLS.clear()

    import tools

    for _, module_name, _ in pkgutil.walk_packages(
        tools.__path__, tools.__name__ + "."
    ):
        if module_name == "tools.decorator":
            continue
        try:
            importlib.import_module(module_name)
        except Exception:  # noqa: BLE001, S110
            pass

    for name, wrapper in _DECORATED_TOOLS.items():
        TOOL_REGISTRY[name] = wrapper
        doc = wrapper.tool_description
        parameters_schema = _build_parameter_schema(wrapper)

        TOOLS.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc,
                    "parameters": parameters_schema,
                },
            }
        )


def execute_tool(name: str, arguments: dict | None = None) -> str:
    print(f"\n[Tool Execution] Calling tool '{name}' with arguments: {arguments}")
    arguments = arguments or {}

    function = TOOL_REGISTRY.get(name)

    if function is None:
        return json.dumps({"error": f"Unknown tool {name!r}"})

    try:
        result = function(**arguments)

        if isinstance(result, str):
            return result

        return json.dumps(result)

    except Exception as error:  # noqa: BLE001
        return json.dumps({"error": str(error)})


load_tools()
