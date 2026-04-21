"""
Test client for the file-structure MCP server.

Usage (from root of memory_map project, with venv active):
    python tests/test_client.py
"""

import asyncio
import json
from fastmcp import Client


SERVER_PATH = "server.py"


async def call_tool(client: Client, tool: str, args: dict, label: str):
    """Call a tool and print its raw minified JSON result."""
    print(f"\n{'=' * 60}")
    print(f"TOOL : {tool}")
    print(f"ARGS : {args}")
    print("=" * 60)

    result = await client.call_tool(tool, args)
    print(result.content[0].text)


async def main():
    async with Client(SERVER_PATH) as client:

        # ------------------------------------------------------------------ #
        # Test 1: Local directory                                              #
        # ------------------------------------------------------------------ #
        await call_tool(
            client,
            tool="get_local_structure",
            args={
                "path": r"C:\Users\Sidhartha\Desktop\memory_map",
                "max_depth": 2,
            },
            label="local",
        )

        # ------------------------------------------------------------------ #
        # Test 2: GitHub repository                                            #
        # ------------------------------------------------------------------ #
        await call_tool(
            client,
            tool="get_github_structure",
            args={
                "repo": "kid-sid/gitSurfStudio",
                "branch": "main",
            },
            label="github",
        )

        # ------------------------------------------------------------------ #
        # Test 3: Git History                                                #
        # ------------------------------------------------------------------ #
        await call_tool(
            client,
            tool="get_git_history",
            args={
                "path": r"C:\Users\Sidhartha\Desktop\memory_map",
                "count": 5,
            },
            label="git_history",
        )


if __name__ == "__main__":
    asyncio.run(main())
