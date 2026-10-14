#!/usr/bin/env python3
"""grok_reset_card.py — 在 Mac 上一条命令查/兑 Grok 重置卡（rate-limit reset token）。

  python3 grok_reset_card.py                         # 查 sub2api 里全部 grok 号（只读）
  python3 grok_reset_card.py --acct 75 qrhojxepcm@outlook.com   # 只查这几个（id 或邮箱）
  python3 grok_reset_card.py --acct 75 --redeem      # 有未过期的卡就兑一张，兑完回查
  python3 grok_reset_card.py --sso-file F            # 不在 sub2api 的号：F 每行 `邮箱----密码----eyJ..` 或裸 SSO

链路：198 sub2api postgres 取 access_token → 管道进 225 → 225 用 curl 走 WARP
（socks5h 127.0.0.1:40000，grok.com 挡机房 IP）打 gRPC-web。token 全程只在管道和
内存里：不落 Mac 盘、不落 225 盘、不进 curl argv（走 `curl -K -` 的 stdin 配置）。

第 0 步固定先跑阴性对照（假 Bearer 必须回 grpc-status 7）：只有这样「status 0 + 空列表」
才能判成「真没卡」，而不是「认证没生效也回空」。对照不过整轮判废、不出结论。

判读（10-14 实测）：
  status 0 + 数据帧长度 0  → NONE，真没卡（成功时 status 在 body 里的 0x80 trailer 帧，HTTP header 里没有）
  status 7 / 16            → AUTH（token 失效 / 无凭据），header 里直接给（trailers-only 响应）
  RedeemReset 9 = 已兑或没额度，3 = token_id 无效
端点/proto 源自 stablyai/orca PR #18116：
  GetRemainingResets 请求为空；响应 repeated ResetToken @10 {tokenId@10, grantedAt@20, expiresAt@30}
  RedeemResetRequest {tokenId@10}；Timestamp {seconds@1}
"""
import argparse
import os
import shlex
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

H198 = os.environ.get("SUB2API_HOST", "cltx@10.68.13.198")
H225 = os.environ.get("XAI_HOST", "cltx@10.68.13.225")
BASE = "https://grok.com/prod_mc_billing.ConsumerUiSvc"
SOCKS = "127.0.0.1:40000"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=no", "-o", "ClearAllForwardings=yes"]

# ── proto / gRPC-web（纯函数，test_grok_reset_card.py 离线覆盖） ──


def varint(n):
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def read_varint(b, i):
    v = shift = 0
    while i < len(b):
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        if not c & 0x80:
            return v, i
        shift += 7
    raise ValueError("truncated varint")


def fields(b):
    """flat proto → [(field, wire, value)]；只认本 API 用到的 varint/len 两种。"""
    out, i = [], 0
    while i < len(b):
        tag, i = read_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = read_varint(b, i)
        elif wt == 2:
            n, i = read_varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            raise ValueError("unexpected wire type %d" % wt)
        out.append((fn, wt, v))
    return out


def frame(payload=b""):
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def unframe(raw):
    """→ (data_bytes, trailers_dict)。成功响应的 grpc-status 只在 0x80 trailer 帧里。"""
    data, trailers, i = b"", {}, 0
    while i + 5 <= len(raw):
        flag, n = raw[i], struct.unpack(">I", raw[i + 1:i + 5])[0]
        body = raw[i + 5:i + 5 + n]
        i += 5 + n
        if flag & 0x80:
            for line in body.decode("utf-8", "replace").split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    trailers[k.strip().lower()] = v.strip()
        else:
            data += body
    return data, trailers


def grpc_status(headers, trailers):
    """header（trailers-only 错误响应）优先，其次 trailer 帧；两处都没有 → None（判 ERR，不当成功）。"""
    for src in (headers, trailers):
        if "grpc-status" in src:
            return src["grpc-status"], src.get("grpc-message", "")
    return None, ""


def ts(b):
    for fn, wt, v in fields(b):
        if fn == 1 and wt == 0 and v:
            return datetime.fromtimestamp(v, tz=timezone.utc)
    return None


def decode_tokens(data):
    toks = []
    for fn, wt, v in fields(data):
        if fn == 10 and wt == 2:
            t = {}
            for f2, w2, v2 in fields(v):
                if f2 == 10 and w2 == 2:
                    t["id"] = v2.decode("utf-8", "replace")
                elif f2 == 20 and w2 == 2:
                    t["granted"] = ts(v2)
                elif f2 == 30 and w2 == 2:
                    t["expires"] = ts(v2)
            toks.append(t)
    return toks


def redeem_req(token_id):
    b = token_id.encode()
    return varint((10 << 3) | 2) + varint(len(b)) + b


REDEEM_MEANING = {"0": "OK", "9": "已兑或没额度", "3": "token_id 无效"}

# ── 225 worker：stdin 每行 `id|name|kind|cred`，kind=bearer|sso；只依赖 stdlib + curl ──


def rpc(cred, kind, method, payload, tmp):
    """一次 gRPC-web调用 → (status, message, data_bytes)。凭据经 `curl -K -` 的 stdin 进，不进 argv。"""
    body_f, hdr_f, req_f = (os.path.join(tmp, n) for n in ("r.bin", "h.txt", "q.bin"))
    with open(req_f, "wb") as f:
        f.write(frame(payload))
    auth = ('header = "Authorization: Bearer %s"\nheader = "X-XAI-Token-Auth: xai-grok-cli"\n' % cred
            if kind == "bearer" else 'cookie = "sso=%s; sso-rw=%s"\n' % (cred, cred))
    cfg = auth + 'header = "Content-Type: application/grpc-web+proto"\nheader = "x-grpc-web: 1"\n'
    p = subprocess.run(
        ["curl", "-s", "-m", "30", "--socks5-hostname", SOCKS, "-X", "POST", "-K", "-",
         "-D", hdr_f, "-o", body_f, "-w", "%{http_code}", "--data-binary", "@" + req_f,
         "%s/%s" % (BASE, method)],
        input=cfg.encode(), capture_output=True)
    code = p.stdout.decode().strip()
    if p.returncode or code != "200":
        return "HTTP%s/curl%d" % (code or "-", p.returncode), "", b""
    headers = {}
    for line in open(hdr_f, encoding="utf-8", errors="replace"):
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    data, trailers = unframe(open(body_f, "rb").read())
    st, msg = grpc_status(headers, trailers)
    return st, msg, data


def worker(redeem):
    tmp = tempfile.mkdtemp(prefix="grokrst.")
    rows = [l.rstrip("\n").split("|", 3) for l in sys.stdin if l.count("|") >= 3]
    try:
        # 第 0 步：阴性对照。假 Bearer 不回 7 ⇒ 尺子坏了，整轮不出结论。
        st, msg, _ = rpc("bogus-negative-control", "bearer", "GetRemainingResets", b"", tmp)
        print("CONTROL bogus-bearer status=%s %s" % (st, "ok" if st == "7" else "BROKEN"), flush=True)
        if st != "7":
            print("ABORT 阴性对照没回 status 7，下面的「空」不可信", flush=True)
            return 2
        now = datetime.now(tz=timezone.utc)
        for aid, name, kind, cred in rows:
            st, msg, data = rpc(cred, kind, "GetRemainingResets", b"", tmp)
            if st != "0":
                print("ROW %s|%s|AUTH|status=%s %s" % (aid, name, st, msg[:80]), flush=True)
                continue
            toks = decode_tokens(data)
            live = [t for t in toks if not t.get("expires") or t["expires"] > now]
            desc = ";".join("%s exp=%s" % (t.get("id", "?"),
                                           t["expires"].strftime("%Y-%m-%d") if t.get("expires") else "?")
                            for t in toks)
            print("ROW %s|%s|%s|total=%d live=%d %s" % (aid, name, "HAS" if live else "NONE",
                                                        len(toks), len(live), desc), flush=True)
            if redeem and live:
                rs, rm, _ = rpc(cred, kind, "RedeemReset", redeem_req(live[0]["id"]), tmp)
                after, _, d2 = rpc(cred, kind, "GetRemainingResets", b"", tmp)
                print("REDEEM %s|%s|%s|status=%s %s %s|after=%d" % (
                    aid, name, live[0]["id"], rs, REDEEM_MEANING.get(rs, ""), rm[:80],
                    len(decode_tokens(d2)) if after == "0" else -1), flush=True)
        return 0
    finally:
        subprocess.run(["rm", "-rf", tmp])


# ── Mac 端编排 ──

SQL = ("select id, name, coalesce(credentials->>'access_token','') from accounts "
       "where platform='grok' and deleted_at is null %s order by id")


def fetch_rows(accts):
    where = ""
    if accts:
        ids = [a for a in accts if a.isdigit()]
        names = [a.replace("'", "''") for a in accts if not a.isdigit()]
        conds = (["id in (%s)" % ",".join(ids)] if ids else []) + \
                (["name in (%s)" % ",".join("'%s'" % n for n in names)] if names else [])
        where = "and (%s)" % " or ".join(conds)
    remote = ("P=$(sudo -n kubectl -n litellm-dev get pod --field-selector=status.phase=Running -o name "
              "| grep sub2api-postgres | head -1); sudo -n kubectl -n litellm-dev exec $P -- "
              "psql -U sub2api -d sub2api -tA -F'|' -c %s" % shlex.quote(SQL % where))
    p = subprocess.run(SSH + [H198, remote], capture_output=True, text=True)
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            rows.append((parts[0], parts[1], "bearer", parts[2]))
    if accts:
        got = {r[0] for r in rows} | {r[1] for r in rows}
        for a in accts:
            if a not in got:
                print("MISS %s 不在 sub2api（或已删除）；网页号用 --sso-file" % a)
    return rows


def sso_rows(path):
    rows = []
    for i, line in enumerate(open(path), 1):
        line = line.strip()
        if not line:
            continue
        parts = line.split("----")
        sso = parts[-1] if parts[-1].startswith("eyJ") else ""
        if sso:
            rows.append(("sso%d" % i, parts[0] if len(parts) > 1 else "sso-line%d" % i, "sso", sso))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--acct", nargs="*", default=[], help="sub2api 账号 id 或邮箱；缺省=全部 grok 号")
    ap.add_argument("--sso-file", help="每行 邮箱----密码----SSO 或裸 SSO（不在 sub2api 的网页号）")
    ap.add_argument("--redeem", action="store_true", help="有未过期卡就兑第一张（不可逆，须显式指定 --acct）")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        sys.exit(worker(a.redeem))
    if a.redeem and not (a.acct or a.sso_file):
        sys.exit("--redeem 必须配 --acct / --sso-file 点名，不对整池兑")

    rows = sso_rows(a.sso_file) if a.sso_file else []
    if a.acct or not a.sso_file:
        rows += fetch_rows(a.acct)
    no_tok = [r for r in rows if not r[3]]
    rows = [r for r in rows if r[3]]
    for r in no_tok:
        print("ROW %s|%s|NOTOKEN|sub2api 里没有 access_token" % (r[0], r[1]))
    if not rows:
        sys.exit("没有可查的号")

    # 脚本本体 scp 到 225 的 mktemp 目录；凭据只走 ssh stdin
    d = subprocess.run(SSH + [H225, "umask 077; mktemp -d /tmp/grokrst.XXXXXX"],
                       capture_output=True, text=True).stdout.strip()
    if not d.startswith("/tmp/grokrst."):
        sys.exit("225 mktemp 失败")
    try:
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", os.path.abspath(__file__),
                        "%s:%s/w.py" % (H225, d)], check=True)
        stdin = "".join("|".join(r) + "\n" for r in rows)
        cmd = "python3 %s/w.py --worker%s" % (d, " --redeem" if a.redeem else "")
        p = subprocess.run(SSH + [H225, cmd], input=stdin, capture_output=True, text=True)
        out = p.stdout
        print(out, end="")
        if p.returncode:
            print(p.stderr[-800:], file=sys.stderr)
            sys.exit(p.returncode)
    finally:
        subprocess.run(SSH + [H225, "rm -rf %s" % shlex.quote(d)])

    kinds = {}
    for line in out.splitlines():
        if line.startswith("ROW "):
            k = line.split("|")[2]
            kinds[k] = kinds.get(k, 0) + 1
    kinds.update({"NOTOKEN": len(no_tok)} if no_tok else {})
    print("SUMMARY %d 号: %s" % (len(rows) + len(no_tok),
                                 " ".join("%s=%d" % kv for kv in sorted(kinds.items()))))


if __name__ == "__main__":
    main()
