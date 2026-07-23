import sys
import anyio
import json
import argparse
from mcp.client.sse import sse_client
from mcp.shared.message import SessionMessage
import mcp.types as types

async def read_stdin(write_stream):
    # wrap sys.stdin using anyio to read lines asynchronously
    # standard sys.stdin is blocking, so we run wrap_file or read in thread
    # anyio.wrap_file is standard for this
    async for line in anyio.wrap_file(sys.stdin):
        line = line.strip()
        if not line:
            continue
        try:
            message = types.JSONRPCMessage.model_validate_json(line)
            await write_stream.send(SessionMessage(message))
        except Exception as e:
            print(f"Error parsing stdin message: {e}", file=sys.stderr)

async def write_stdout(read_stream):
    async for session_message in read_stream:
        try:
            msg_json = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
            sys.stdout.write(msg_json + "\n")
            sys.stdout.flush()
        except Exception as e:
            print(f"Error writing to stdout: {e}", file=sys.stderr)

async def main():
    parser = argparse.ArgumentParser(description="Python-based SSE-to-stdio MCP client bridge")
    parser.add_argument("url", help="SSE endpoint URL")
    parser.add_argument("--header", action="append", help="HTTP header in 'Key: Value' format")
    args = parser.parse_args()

    headers = {}
    if args.header:
        for h in args.header:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()

    async with sse_client(args.url, headers=headers) as (read_stream, write_stream):
        async with anyio.create_task_group() as tg:
            tg.start_soon(read_stdin, write_stream)
            tg.start_soon(write_stdout, read_stream)

if __name__ == "__main__":
    anyio.run(main)
