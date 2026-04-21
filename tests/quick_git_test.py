import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastmcp import Client
from server import mcp

async def main():
    async with Client(mcp) as client:
        result = await client.call_tool('get_git_history', {'path': r'C:\Users\Sidhartha\Desktop\memory_map', 'count': 5})
        print(result.content[0].text)

if __name__ == "__main__":
    asyncio.run(main())
