"""A tiny MCP server (stdio) for engine tests: one read-only tool and one that changes something."""
from fastmcp import FastMCP

mcp = FastMCP("test")
STATE = {"value": 0}


@mcp.tool(annotations={"readOnlyHint": True})
def whoami() -> dict:
    """Who is connected."""
    return {"username": "tester", "companies": ["acme"]}


@mcp.tool()
def set_value(value: int) -> str:
    """Change the stored value."""
    STATE["value"] = value
    return f"value is now {value}"


if __name__ == "__main__":
    mcp.run()
