import os
import subprocess
import sys


def test_stuck_executor_cannot_hold_owned_service_forever():
    script = '''
import asyncio, threading
from mcp_second_brain.query_dispatch import QueryDispatcher
from mcp_second_brain.shutdown_guard import arm_shutdown_exit
async def main():
    dispatcher = QueryDispatcher(timeout=.05)
    try:
        await dispatcher.run('a', threading.Event().wait)
    except TimeoutError:
        pass
    assert not await dispatcher.close(timeout=.05)
    arm_shutdown_exit(seconds=.1)
asyncio.run(main())
'''
    process = subprocess.Popen([sys.executable, '-c', script], env={'PATH': os.defpath, 'PYTHONPATH': os.getcwd()}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert process.wait(timeout=3) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
