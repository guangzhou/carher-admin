"""poller 的 Cursor 支 → 真账户库进程 → 假网关:issueCursorKeyViaStore 端到端(不碰 188 / 198)。

poller 代码原样 require(仓库里那份),只给 node 一个进程环境变量指向本地账户库。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import account_store as AS  # noqa: E402
import test_issue as TI  # noqa: E402

POLLER = HERE.parent / "hermestest-cron" / "litellm-key-poller.js"
TOKEN = "ast_poller-test-" + "z" * 24


@pytest.fixture
def stack(tmp_path):
    if not shutil.which("node"):
        pytest.skip("没有 node")
    TI.G.reset()
    gw = ThreadingHTTPServer(("127.0.0.1", 0), TI.FakeGW)
    gw.daemon_threads = True
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    for d in ("data", "kek", "gw"):
        (tmp_path / d).mkdir()
    db, kek = tmp_path / "data/a.db", tmp_path / "kek/kek.bin"
    AS.gen_kek(kek)
    (tmp_path / "gw/master_key").write_text(TI.MASTER)
    tok = tmp_path / "tokens.json"
    tok.write_text(json.dumps({"clients": {
        "poller": {"sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "scopes": ["issue", "reveal:issued"]}}}))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, ACCOUNT_STORE_DB=str(db), ACCOUNT_STORE_KEK=str(kek), ACCOUNT_STORE_TOKENS=str(tok),
               LISTEN=f"127.0.0.1:{port}", ACCOUNT_STORE_GATEWAY=f"http://127.0.0.1:{gw.server_port}/pro",
               ACCOUNT_STORE_GATEWAY_KEY_FILE=str(tmp_path / "gw/master_key"))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "db": db}
    proc.terminate()
    out = proc.communicate(timeout=5)[0]
    gw.shutdown()
    leaked = [k["plain"] for k in TI.G.keys.values() if k["plain"] in out]
    assert not leaked and TI.MASTER not in out, "账户库日志里不能有明文 / master key"


def run_poller(stack, union_id, token=TOKEN, url=None):
    js = ("const p=require(%s);" % json.dumps(str(POLLER)) +
          "p.issueCursorKeyViaStore(p.loadAccountStoreConfig(),'chenyisheng',"
          "{recordId:'recFORM0001',name:'陈一圣',openId:'ou_applicant01',email:'chenyisheng@auto-link.com.cn',"
          "jobTitle:'算法工程师',department:'AI 工程部',organization:'无锡车联'},%s)" % json.dumps(union_id) +
          ".then(r=>process.stdout.write(JSON.stringify({ok:1,existing:r.existing,keyTail:r.key.slice(-4),"
          "keyLen:r.key.length,budget:r.reference.max_budget,models:r.reference.models,id:r.accountId})))"
          ".catch(e=>process.stdout.write(JSON.stringify({ok:0,err:String(e.message)})))")
    env = dict(os.environ, ACCOUNT_STORE_URL=url or stack["url"], ACCOUNT_STORE_TOKEN=token)
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, env=env, timeout=60)
    return json.loads(r.stdout or "{}")


def test_poller_cursor_branch_issues_via_store(stack):
    r = run_poller(stack, TI.UID_N)
    assert r["ok"] == 1 and r["existing"] is False, r
    gens = [c for c in TI.G.calls if c[1].endswith("/key/generate")]
    assert len(gens) == 1
    sent = gens[0][2]
    plain = TI.G.keys[sent["key_alias"]]["plain"]
    assert r["keyTail"] == plain[-4:] and r["keyLen"] == len(plain), "poller 拿到的是网关发出的那把"
    assert r["budget"] == TI.REFERENCE["max_budget"] and r["models"] == TI.REFERENCE["models"]
    assert sent["metadata"]["source_record_id"] == "recFORM0001" and sent["team_id"] == "t-new"
    # 再跑一次(同一人第二张表单):复用,不再调网关
    r2 = run_poller(stack, TI.UID_N)
    assert r2["ok"] == 1 and r2["existing"] is True and r2["keyTail"] == r["keyTail"]
    assert len([c for c in TI.G.calls if c[1].endswith("/key/generate")]) == 1


def test_poller_fails_closed(stack):
    assert "not configured" in run_poller(stack, TI.UID_N, token="")["err"]
    assert "no union_id" in run_poller(stack, "ou_not_union")["err"]
    assert "account-store 401" in run_poller(stack, TI.UID_N, token="wrong-token-0000000000")["err"]
    assert not [c for c in TI.G.calls if c[1].endswith("/key/generate")], "失败关闭:一个都不发"
    assert run_poller(stack, TI.UID_N)["ok"] == 1, "POSITIVE:配对了就能发"
