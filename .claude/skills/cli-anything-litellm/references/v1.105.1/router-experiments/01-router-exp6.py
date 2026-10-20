"""Which failure handlers does ResponsesAPIStreamingIterator._handle_failure reach, upstream vs our patch? Offline."""
import asyncio, json, os, sys, time
from datetime import datetime
import litellm
from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator as RSI

class Log:
    def __init__(self): self.calls = []
    async def async_failure_handler(self, **kw): self.calls.append("async_failure_handler")
    def failure_handler(self, *a, **kw): self.calls.append("failure_handler(sync: router cooldown lives here)")

def mk():
    o = RSI.__new__(RSI)
    o._failure_handled = False; o.logging_obj = Log(); o.start_time = datetime.now(); o._pending_logging_tasks = ()
    return o

async def probe(label):
    o = mk()
    o._handle_failure(RuntimeError("x"))
    await asyncio.sleep(0.5)
    await asyncio.get_running_loop().run_in_executor(None, time.sleep, 0.3)
    print(json.dumps({"variant": label, "handlers_called": sorted(set(o.logging_obj.calls))}))

async def main():
    await probe("upstream v1.105.1")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live-cb"))
    import streaming_output_backfill  # noqa: F401  installs _patched_handle_failure
    await probe("with streaming_output_backfill (prod)")

asyncio.run(main())
