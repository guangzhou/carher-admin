# 构建期行为断言：被取消的 create_connection(sock=) 之后 socket 必须已 detach（0.22.1 这里 fileno() 仍是原号 ⇒ 日后 GC 双关）
import asyncio, socket, uvloop
from importlib.metadata import version
assert uvloop.__version__ == "0.23.0" and version("uvloop") == "0.23.0", (uvloop.__version__, version("uvloop"))

async def main():
    loop = asyncio.get_running_loop()
    srv = socket.create_server(("127.0.0.1", 0))
    for _ in range(20):
        c = socket.socket(); c.setblocking(False)
        try: c.connect(srv.getsockname())
        except BlockingIOError: pass
        t = asyncio.ensure_future(loop.create_connection(asyncio.Protocol, sock=c))
        await asyncio.sleep(0); t.cancel()
        try: await t
        except BaseException: pass
        assert c.fileno() == -1, "uvloop still owns fd after cancelled create_connection"
    srv.close()

uvloop.run(main())
print("uvloop 0.23.0 detach-on-cancel OK")
