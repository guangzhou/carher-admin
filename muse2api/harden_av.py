#!/usr/bin/env python3
"""Harden patches #9 (audio/video input) and #10 (OpenAI-shaped /v1/videos) for muse2api.

Kept separate from harden.py on purpose: this is one cohesive feature block that can be
dropped (stop running this file) without touching the eight patches that keep the pinned
deployment safe. Same contract as harden.py -- every edit must match exactly once, so an
upstream change can't silently skip a patch.

Run AFTER harden.py on the same checkout:  python3 harden_av.py <checkout>
deploy-198.sh does this for you.

#9  audio / video input (measured on muse-horwich-04, hatch_vm, free plan, 2026-10-29)
    * STT  POST muse.ai/api/hatch/voice/voyager-token (cookies) -> {"token"}
           wss://shortwave.meta.ai/voyager/v1/asr/duplex?sessionId=<uuid>
           send {"mode":"DEFAULT","authorization":{"accessToken":tok},"audioEncoding":"PCM_16KHZ","model":"prod"}
           <- {"sessionId"} ack, then binary PCM s16le 16k mono, then {"endStream":{}}
           <- {"transcript":{"transcript","final",...}} / {"audioProgress":..} / {"error":..}
           No browser, no chat turn, so it takes no generation slot. zh/en both correct.
           A long stream pushed at 6x/20x is rejected ("Audio processing backlog too large");
           3x passes, 1x is what the web client does. Sporadic doubled words ("大家大家")
           happen at every speed including 1x -- a Voyager trait, not our pacing. So:
           send at 1.0x, split long audio at quiet points, run chunks in parallel (4 OK).
    * Video/audio understanding: the composer accepts video/* and audio/wav; the reply takes
           20~50 s and is a normal chat turn (it does count against the weekly quota).
           🔴 The agent otherwise goes off and runs VM tasks (it tried to pip-install
           faster-whisper and to query a calendar on 2026-10-29) -- _AV_PROMPT_SUFFIX is the
           guard, and it was re-measured with the transcript prompt that triggered that:
           reply in 24 s, no approval/VM UI at all.
    * 🔴 Video speech is NOT transcribed by the attachment path: asked to transcribe the
           balloon clip, muse answered "这个视频里没有人说话" although the track has speech
           at -19 dB mean. Attachments are for understanding; transcription goes to Voyager.
    * TTS  muse.ai /api/hatch/voice/tts(/stream) answers 404 for our accounts (voice gate
           572a7c97=false, vm_type hatch_vm), so /v1/audio/speech returns 501 with the reason
           instead of pretending.

#10 /v1/videos through LiteLLM. LiteLLM's standard video route is not optional-shaped:
    * POST is unconditionally multipart/form-data (upstream PR #38104, merged 2026-08-24,
      before every version we run -- waiting for a release never fixes it), and
    * both POST and GET responses go through VideoObject.model_validate, which demands
      id + object=="video" (a Literal) + status, and error as an object, not a string.
    Upstream muse2api answers JSON-only and returns object="video.task" with a string error,
    so the row registered fine and could never be called. This patch makes the three task
    endpoints speak the OpenAI shape (and adds /content so the mp4 can be downloaded through
    LiteLLM) while keeping every field the admin page and direct :30610 callers already read.
"""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1])


def patch(rel, old, new):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n != 1:
        sys.exit(f"[harden_av] {rel}: expected 1 match, got {n}: {old[:60]!r}")
    p.write_text(text.replace(old, new), encoding="utf-8")
    print(f"[harden_av] {rel}: ok")


# 9a. ffmpeg in the image: decode any client audio/video to 16k mono PCM for Voyager.
patch(
    "Dockerfile",
    "    chromium \\\n",
    "    chromium \\\n"
    "    ffmpeg \\\n",
)

# 9b. engine: Voyager ASR client. Pure HTTP + WebSocket, no browser, and it leaves from the
#     same per-account exit as the chat WebSocket (patch 7c's rule: one IP per account per turn).
patch(
    "engine.py",
    "    def _attach_image(self, image_data: str):\n",
    r'''    # ---------------- Speech-to-text (Voyager, no browser) ----------------
    VOYAGER_WS = "wss://shortwave.meta.ai/voyager/v1/asr/duplex?sessionId="
    VOYAGER_CHUNK = 8192  # 256 ms of 16 kHz s16le, same size the web ScriptProcessor sends

    # 🔴 听写（voyager）走**共用隧道**，不跟一号一 IP —— 10-30 实测：M1 上线后
    #    同一批 cookie 走各号自己的新出口 11/12 回 404，走 pod 默认隧道 12/12 200；
    #    出口本身都通（ipify 对得上）⇒ 是 muse.ai 只认部分出口 IP。
    #    token 和 WS 必须同一条腿，所以两处一起改。续签/聊天/生成仍按号分 IP。
    def voyager_token(self, cookies: dict, account_id: str | None = None) -> str:
        import requests
        if os.environ.get("MUSE2API_ASR_PER_ACCOUNT_EGRESS") == "1":
            configured, bridge = self.proxy_for_account(account_id)
            proxy = bridge or configured
        else:
            proxy = os.environ.get("MUSE2API_EGRESS_PROXY", "") or os.environ.get("ALL_PROXY", "")
        kw = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
        try:
            r = requests.post("https://muse.ai/api/hatch/voice/voyager-token", timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                "Origin": "https://muse.ai", "Referer": "https://muse.ai/thread/new",
                "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
                "Cookie": "; ".join(f"{k}={v}" for k, v in (cookies or {}).items() if v)}, **kw)
        except requests.RequestException as exc:
            # Never echo the exception text: it can carry the proxy URL with credentials.
            raise MuseGenerationError(
                f"voyager-token 网络请求失败 ({type(exc).__name__})；请检查出口代理后重试") from None
        if r.status_code == 401:
            raise MuseAuthError("voyager-token HTTP 401：会话失效")
        tok = (r.json() or {}).get("token") if r.status_code == 200 else None
        if not tok:
            raise MuseGenerationError(f"voyager-token HTTP {r.status_code}：未拿到听写 token")
        return tok

    def _voyager_socket(self, account_id: str | None):
        """TLS socket to shortwave.meta.ai through this account's exit (socks5h or HTTP CONNECT)."""
        import ssl
        if os.environ.get("MUSE2API_ASR_PER_ACCOUNT_EGRESS") == "1":
            configured, bridge = self.proxy_for_account(account_id)
            proxy = bridge or configured or os.environ.get("ALL_PROXY", "")
        else:
            # 和 voyager_token 必须同一条腿：token 在 A IP 签发、WS 从 B IP 连会被拒
            proxy = os.environ.get("MUSE2API_EGRESS_PROXY", "") or os.environ.get("ALL_PROXY", "")
        host = "shortwave.meta.ai"
        if not proxy:
            raw = socket.create_connection((host, 443), timeout=30)
        else:
            import socks  # PySocks, pulled in by requests[socks]
            u = urllib.parse.urlparse(proxy)
            ptype = socks.SOCKS5 if u.scheme.startswith("socks5") else socks.HTTP
            try:
                raw = socks.create_connection(
                    (host, 443), timeout=30, proxy_type=ptype, proxy_addr=u.hostname,
                    proxy_port=u.port, proxy_rdns=True,
                    proxy_username=u.username and urllib.parse.unquote(u.username),
                    proxy_password=u.password and urllib.parse.unquote(u.password))
            except Exception as exc:
                raise MuseGenerationError(
                    f"听写出口代理连接失败 ({type(exc).__name__})") from None
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)

    def voyager_transcribe(self, cookies: dict, pcm: bytes, account_id: str | None = None,
                           token: str | None = None, speed: float = 1.0, timeout: float = 120.0) -> str:
        """Stream 16 kHz mono s16le PCM to Meta's Voyager ASR and return the final text.

        speed=1.0 mirrors the web client. 6x was rejected ("Audio processing backlog too
        large"), 3x passed -- split long audio and run chunks in parallel instead of
        speeding up. Output may contain doubled words; that is Voyager, not us.
        """
        import websocket
        tok = token or self.voyager_token(cookies, account_id)
        sid = str(uuid.uuid4())
        ws = websocket.create_connection(self.VOYAGER_WS + sid, origin="https://muse.ai", timeout=30,
                                         socket=self._voyager_socket(account_id))
        finals, errors = [], []
        try:
            ws.send(json.dumps({"mode": "DEFAULT", "authorization": {"accessToken": tok},
                                "audioEncoding": "PCM_16KHZ", "model": "prod"}))
            ack = json.loads(ws.recv())
            if ack.get("error") or ack.get("sessionId") != sid:
                raise MuseGenerationError(
                    f"Voyager 握手失败: {(ack.get('error') or {}).get('message', 'bad ack')}")

            def rx():
                # Only this thread ever calls recv(); the sender below only calls
                # send_binary/send, and websocket-client locks the two separately.
                while True:
                    try:
                        m = ws.recv()
                    except Exception:
                        return
                    if not m:
                        return
                    if isinstance(m, bytes):
                        continue
                    try:
                        j = json.loads(m)
                    except ValueError:
                        continue
                    if j.get("error"):
                        errors.append(j["error"].get("message") or "Voyager error")
                        return
                    tr = j.get("transcript")
                    if tr and tr.get("final"):
                        finals.append(tr.get("transcript") or "")
            th = threading.Thread(target=rx, daemon=True)
            th.start()
            step = self.VOYAGER_CHUNK / 32000.0 / max(0.25, min(speed, 3.0))
            for i in range(0, len(pcm), self.VOYAGER_CHUNK):
                if errors:
                    break
                ws.send_binary(pcm[i:i + self.VOYAGER_CHUNK])
                time.sleep(step)
            if not errors:
                ws.send(json.dumps({"endStream": {}}))
            th.join(timeout)
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if errors:
            raise MuseGenerationError("Voyager 听写失败: " + errors[0])
        # Voyager ends each final with a doubled full stop ("..", "。。").
        return re.sub(r"([.。])\1+$", r"\1", " ".join(t.strip() for t in finals if t.strip()))

    # ---------------- Generic composer attachments (video / audio) ----------------
    CHAT_ATTACH_MAX_BYTES = 40 * 1024 * 1024  # composer maxFileSize 0x2800000, video <= 120 s

    def _attach_files(self, files: list[dict]):
        """files: [{"b64","mime","name"}]. Measured: video/* and audio/wav are accepted here."""
        if not files:
            return
        self._clear_attachments()
        js = r"""(async function(files){
          var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
          if (!inputs.length) return JSON.stringify({ok:false,err:'no-file-input'});
          var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
          var composer = ov ? ov.closest('form') : null;
          var inp = (composer && inputs.find(function(x){ return composer.contains(x); })) || inputs[0];
          inp.removeAttribute('accept');
          for (var f of files){ var bin=atob(f.b64), u8=new Uint8Array(bin.length);
            for (var i=0;i<bin.length;i++) u8[i]=bin.charCodeAt(i);
            var dt=new DataTransfer(); dt.items.add(new File([u8], f.name, {type:f.mime}));
            inp.files=dt.files; inp.dispatchEvent(new Event('change',{bubbles:true}));
            await new Promise(function(r){ setTimeout(r, 350); }); }
          return JSON.stringify({ok:true,count:files.length});
        })(%s)"""
        raw = self.page.js(js % json.dumps(files), await_promise=True, timeout=180)
        res = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not res.get("ok"):
            raise MuseGenerationError(f"附件注入失败: {res.get('err')}")
        # A historical thumbnail proves nothing; wait for this turn's remove buttons.
        deadline = time.time() + 90.0
        seen = 0
        while time.time() < deadline:
            seen = int(self.page.js(r"""(function(){
              var ov=document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
              var c=ov?(ov.closest('form')||ov.parentElement.parentElement.parentElement):null;
              if(!c) return 0;
              return [...c.querySelectorAll('button[aria-label]')].filter(b=>/remove\s*attachment|移除附件|删除附件/i
                .test(b.getAttribute('aria-label')||'')).length;})()""") or 0)
            if seen >= len(files):
                time.sleep(1.0)
                return
            time.sleep(0.4)
        raise MuseGenerationError(
            f"附件上传未确认（{seen}/{len(files)}，composer 未出现移除按钮），已停止发送")

    def _attach_image(self, image_data: str):
''',
)

# 9c. chat_stream takes attachments. A media turn needs much longer than 40 s to the first
#     token (19~49 s measured, 150 s headroom), and an attachment means the hot page must be
#     reset first (reset_thread(for_chat=True) reuses a page that still holds one).
patch(
    "engine.py",
    "    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,\n"
    "                    timeout: int | None = None, account_id: str | None = None,\n"
    "                    stop_event=None):\n",
    "    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,\n"
    "                    timeout: int | None = None, account_id: str | None = None,\n"
    "                    stop_event=None, attachments: list[dict] | None = None):\n",
)
patch(
    "engine.py",
    "        self.reset_thread(for_chat=True)\n"
    "        base_agent, base_text, _ = self._poll_chat()\n"
    "        self._send(prompt)\n",
    "        self.reset_thread(for_chat=not attachments)\n"
    "        if attachments:\n"
    "            self._attach_files(attachments)\n"
    "        base_agent, base_text, _ = self._poll_chat()\n"
    "        self._send(prompt)\n",
)
patch(
    "engine.py",
    "        first_token_deadline = min(deadline, t_sent + 40.0)\n",
    "        first_token_deadline = min(deadline, t_sent + (150.0 if attachments else 40.0))\n",
)

# 9d. safe_chat_stream forwards attachments to the engine (every existing caller passes none).
patch(
    "app.py",
    "def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,\n"
    "                     timeout: int, account_id: str | None):\n",
    "def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,\n"
    "                     timeout: int, account_id: str | None, attachments: list[dict] | None = None):\n",
)
patch(
    "app.py",
    "                        for chunk in engine.chat_stream(\n"
    "                            cur_cookies, prompt, cur_exp, timeout,\n"
    "                            account_id=cur_id, stop_event=stop_event\n"
    "                        ):\n",
    "                        for chunk in engine.chat_stream(\n"
    "                            cur_cookies, prompt, cur_exp, timeout,\n"
    "                            account_id=cur_id, stop_event=stop_event,\n"
    "                            attachments=attachments\n"
    "                        ):\n",
)

# 9e. Chat with audio/video content parts -> attach them to the composer.
#     Only inline data: URIs are accepted, so there is no server-side fetch and no SSRF surface
#     (the reference-image path has its own public-IP check; this one simply never fetches).
#     Placed before the muse-image branch from patch #4 so a media turn is never sent to the
#     image pipeline, and before the empty-prompt 400 so an attachment-only request is legal.
patch(
    "app.py",
    "    prompt = build_chat_prompt(req.messages) or (req.prompt or \"\").strip()\n"
    "    if not prompt:\n"
    "        raise HTTPException(400, \"messages 为空\")\n"
    "    if resolve_model(req.model, default=\"muse-spark\") == \"muse-image\":\n",
    "    prompt = build_chat_prompt(req.messages) or (req.prompt or \"\").strip()\n"
    "    media = _chat_media_parts(req.messages)\n"
    "    if media:\n"
    "        return await _chat_with_media(req, prompt or \"请分析这个文件。\", media)\n"
    "    if not prompt:\n"
    "        raise HTTPException(400, \"messages 为空\")\n"
    "    if resolve_model(req.model, default=\"muse-spark\") == \"muse-image\":\n",
)

patch(
    "app.py",
    "@app.post(\"/v1/chat/completions\")\n"
    "async def chat_completions(req: ChatRequest, _=Depends(auth)):\n",
    r'''# ------------------------- audio / video input (harden #9) -------------------------
import subprocess as _sp
from concurrent.futures import ThreadPoolExecutor as _TPE
from fastapi import File as _File, Form as _Form, UploadFile as _UploadFile

# 🔴 Without this, muse goes and does the job on its cloud VM instead of answering:
#    on 2026-10-29 it pip-installed faster-whisper and queried a calendar. Re-measured with
#    the exact transcript prompt that triggered that: reply in 24 s, no approval/VM UI.
_AV_PROMPT_SUFFIX = ("\n\n（只根据附件本身回答，不要调用任何工具、不要安装软件、"
                     "不要联网搜索、不要创建任何任务或审批。）")
_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm", "video/3gpp"}
_AUDIO_EXT = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4",
              "ogg": "audio/ogg", "webm": "audio/webm", "flac": "audio/flac",
              "aac": "audio/aac", "opus": "audio/opus"}
_AV_MAX_UPLOAD = 200 * 1024 * 1024


def _ffmpeg(args: list[str], data: bytes, timeout: int = 120) -> bytes:
    try:
        p = _sp.run(["ffmpeg", "-v", "error", "-i", "pipe:0", *args, "pipe:1"], input=data,
                    capture_output=True, timeout=timeout)
    except _sp.TimeoutExpired:
        raise HTTPException(400, "音视频解码超时（文件过长或已损坏）") from None
    except FileNotFoundError:
        raise HTTPException(500, "镜像里没有 ffmpeg（harden #9a 未生效）") from None
    if p.returncode != 0 or not p.stdout:
        raise HTTPException(400, "音视频解码失败: " + p.stderr.decode("utf-8", "ignore")[-200:])
    return p.stdout


def _data_uri(value: str) -> tuple[bytes, str]:
    m = re.match(r"^data:([\w.+-]+/[\w.+-]+)?(;[^,]*)?,(.*)$", value or "", re.S)
    if not m or "base64" not in (m.group(2) or ""):
        raise HTTPException(400, "音视频只接受 data:<mime>;base64,... 内联数据（不抓取远程 URL）")
    try:
        return base64.b64decode(m.group(3), validate=False), (m.group(1) or "").lower()
    except (binascii.Error, ValueError):
        raise HTTPException(400, "音视频 base64 无效") from None


def _chat_media_parts(messages) -> list[dict]:
    """Collect input_audio / video_url / file(data:) parts from the LAST user message."""
    last = next((m for m in reversed(messages or []) if (m.role or "").lower() == "user"), None)
    if not last or not isinstance(last.content, list):
        return []
    out = []
    for item in last.content:
        if not isinstance(item, dict):
            continue
        t, raw, mime, name = item.get("type"), None, "", None
        if t == "input_audio":
            ia = item.get("input_audio") or {}
            fmt = (ia.get("format") or "wav").lower()
            try:
                raw = base64.b64decode(ia.get("data") or "")
            except (binascii.Error, ValueError):
                raise HTTPException(400, "input_audio.data base64 无效") from None
            mime = _AUDIO_EXT.get(fmt, "audio/" + fmt)
        elif t in ("video_url", "file", "image_url"):
            src = ((item.get(t) or {}).get("url") if t != "file"
                   else (item.get("file") or {}).get("file_data")) or ""
            if not src.startswith("data:video/") and not src.startswith("data:audio/"):
                continue  # images keep the existing path; remote URLs are never fetched
            raw, mime = _data_uri(src)
            name = (item.get("file") or {}).get("filename") if t == "file" else None
        else:
            continue
        if not raw:
            raise HTTPException(400, "音视频内容为空")
        if len(raw) > _AV_MAX_UPLOAD:
            raise HTTPException(400, "单个附件超过 200 MiB")
        if mime.startswith("audio/") and mime != "audio/wav":
            # The composer accepted .wav in tests; everything else is normalised to wav.
            raw, mime = _ffmpeg(["-vn", "-ac", "1", "-ar", "16000", "-f", "wav"], raw), "audio/wav"
        if not (mime == "audio/wav" or mime in _VIDEO_MIMES):
            raise HTTPException(400, f"不支持的附件类型 {mime}（视频 mp4/mov/webm/3gp，音频会转成 wav）")
        if len(raw) > MuseEngine.CHAT_ATTACH_MAX_BYTES:
            raise HTTPException(400, "附件超过 40 MiB（muse composer 上限）")
        ext = "wav" if mime == "audio/wav" else {"video/quicktime": "mov",
                                                 "video/3gpp": "3gp"}.get(mime, mime.split("/")[1])
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", (name or ""))[:60]
        out.append({"b64": base64.b64encode(raw).decode(), "mime": mime,
                    "name": safe or f"input_{len(out) + 1}.{ext}"})
    if len(out) > 4:
        raise HTTPException(400, "一次最多 4 个音视频附件")
    return out


async def _chat_with_media(req: "ChatRequest", prompt: str, media: list[dict]):
    acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")
    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or max(CFG.chat_timeout, 360))
    full = prompt + _AV_PROMPT_SUFFIX
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

    if req.stream:
        def sync_stream():
            yield _sse(_chat_chunk(cid, created, model, {"role": "assistant", "content": ""}))
            try:
                for chunk in safe_chat_stream(acc["cookies"], full, acc.get("cookies_exp"), timeout,
                                              account_id=acc["id"], attachments=media):
                    for piece in _pace_text(chunk):
                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                store.mark(acc["id"], True, "")
            except MuseAuthError as exc:
                store.mark(acc["id"], False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except Exception as exc:  # noqa: BLE001
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            if _want_usage(req.stream_options):
                yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                            "model": model, "choices": [],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
            yield "data: [DONE]\n\n"
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(acc["cookies"], full, acc.get("cookies_exp"), timeout,
                                        account_id=acc["id"], attachments=media))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def _split_pcm(pcm: bytes, target_s: int = 30, search_s: float = 3.0) -> list[bytes]:
    """Cut 16k s16le PCM near every target_s at the quietest 100 ms window (fewer split words)."""
    import array
    rate, out, start = 32000, [], 0
    while len(pcm) - start > (target_s + search_s) * rate:
        lo, hi = start + int((target_s - search_s) * rate), start + int((target_s + search_s) * rate)
        best, best_e = hi, None
        for pos in range(lo, hi, 3200):
            w = array.array("h", pcm[pos:pos + 3200])
            e = sum(abs(x) for x in w[::8])
            if best_e is None or e < best_e:
                best, best_e = pos, e
        best -= best % 2
        out.append(pcm[start:best])
        start = best
    out.append(pcm[start:])
    return [c for c in out if len(c) >= 3200]


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(file: _UploadFile = _File(...), model: str = _Form("muse-asr"),
                               language: str | None = _Form(None),
                               response_format: str = _Form("json"),
                               _=Depends(auth)):
    """OpenAI-compatible STT via Meta Voyager (the muse composer's own dictation backend).

    No browser and no chat turn, so it takes no generation slot. Whether it draws on the
    weekly quota is unknown -- the free plan exposes no STT counter. `language` is accepted
    and echoed but ignored: Voyager auto-detects zh/en inside one stream.
    """
    if response_format not in ("json", "text", "verbose_json"):
        raise HTTPException(400, "response_format 只支持 json / text / verbose_json")
    data = await file.read()
    if not data:
        raise HTTPException(400, "file 为空")
    if len(data) > _AV_MAX_UPLOAD:
        raise HTTPException(400, "file 超过 200 MiB")
    pcm = await asyncio.to_thread(_ffmpeg, ["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le"], data, 300)
    if len(pcm) < 3200:
        raise HTTPException(400, "音频里没有可听写的声音（解码后不足 0.1 秒）")
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")
    chunks = _split_pcm(pcm)

    def run() -> str:
        tok = engine.voyager_token(acc["cookies"], acc["id"])
        with _TPE(max_workers=min(4, len(chunks))) as ex:
            parts = list(ex.map(
                lambda c: engine.voyager_transcribe(acc["cookies"], c, acc["id"], token=tok,
                                                    timeout=len(c) / 32000 + 60), chunks))
        return " ".join(p for p in parts if p)

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        store.mark(acc["id"], False, str(exc))
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    if response_format == "text":
        return Response(text, media_type="text/plain; charset=utf-8")
    body = {"text": text}
    if response_format == "verbose_json":
        body.update(task="transcribe", language=language or "auto",
                    duration=round(len(pcm) / 32000, 2))
    return body


@app.post("/v1/audio/speech")
async def audio_speech(_=Depends(auth)):
    # muse.ai /api/hatch/voice/tts(/stream) answers 404 for our accounts (voice gate off,
    # vm_type hatch_vm). Fail fast and say why instead of pretending.
    raise HTTPException(501, "muse 账号未开通语音（TTS 接口 404），/v1/audio/speech 暂不可用")


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):
''',
)

# 10. OpenAI-shaped /v1/videos so LiteLLM's standard video route can actually call it.
#     Three independent incompatibilities, all read out of the running litellm image
#     (llms/openai/videos/transformation.py + types/videos/main.py):
#       a) POST is unconditionally multipart/form-data (PR #38104) -> FastAPI's JSON body
#          model answers 422. Accept both encodings.
#       b) POST and GET responses go through VideoObject.model_validate, which requires
#          object == "video" (a Literal), a str id, a str status, and error as an object or
#          null. Upstream answers object="video.task" and error="...": both reject.
#       c) LiteLLM GETs /v1/videos/{id} and downloads /v1/videos/{id}/content -- the latter
#          did not exist, so a client that follows the OpenAI flow could never fetch the mp4.
#     Status words: OpenAI uses queued/in_progress/completed/failed. Our own admin page and
#     the old direct callers read queued/processing/completed/succeeded/failed, so every
#     response carries both ("status" OpenAI-shaped, "task_status" the original) plus the
#     url/result/video/progress fields they already use.
patch(
    "app.py",
    "@app.post(\"/v1/videos\")\n"
    "@app.post(\"/v1/videos/generations\")\n"
    "async def create_video(req: VideoRequest, _=Depends(auth)):\n"
    "    ref_images = await asyncio.to_thread(\n",
    r'''_VIDEO_OPENAI_STATUS = {"queued": "queued", "processing": "in_progress",
                        "running": "in_progress", "completed": "completed",
                        "succeeded": "completed", "success": "completed", "done": "completed",
                        "failed": "failed", "error": "failed"}


def _video_openai_view(t: dict) -> dict:
    """One task -> a body that satisfies litellm's VideoObject AND the old direct callers.

    VideoObject requires id:str, object=="video", status:str and error:dict|None; extra keys
    are kept because LiteLLMBaseModel does not forbid them, so the admin page still finds
    result/url/video/progress/elapsed.
    """
    out = dict(t)
    raw = str(out.get("status") or "queued").lower()
    out["task_status"] = raw
    out["status"] = _VIDEO_OPENAI_STATUS.get(raw, raw)
    res = out.get("result") if isinstance(out.get("result"), dict) else {}
    vurl = out.get("url") or res.get("url")
    if vurl:
        out["url"] = vurl
        out.setdefault("video", {"url": vurl})
    if out["status"] == "completed":
        out["progress"] = 100
    elif out["status"] == "in_progress":
        # Progress reports observed milestones only; Muse exposes no percentage.
        out["progress"] = max(int(out.get("progress") or 0), 15)
    err = out.get("error")
    # VideoObject.error is dict | None -- a string here fails validation inside litellm.
    out["error"] = {"message": err, "code": "upstream_error"} if isinstance(err, str) and err else (
        err if isinstance(err, dict) else None)
    out["object"] = "video"
    out["model"] = out.get("model") or "muse-video"
    out["seconds"] = str(out.get("seconds") or out.get("duration") or "")
    out.setdefault("created_at", int(time.time()))
    if out["status"] == "completed":
        out.setdefault("completed_at", int(out.get("updated_at") or time.time()))
    return out


async def _video_request_from(request: Request) -> VideoRequest:
    """LiteLLM always sends multipart/form-data on /v1/videos (upstream PR #38104);
    direct callers and the admin page send JSON. Accept both."""
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        flat: dict = {}
        for k in form.keys():
            v = form.getlist(k)
            # A multipart file part (input_reference) is a starlette UploadFile.
            vals = [await x.read() if hasattr(x, "read") else x for x in v]
            vals = [base64.b64encode(x).decode() if isinstance(x, bytes) else x for x in vals]
            flat[k] = vals[0] if len(vals) == 1 else vals
        # OpenAI's field names -> ours.
        if "seconds" in flat and "duration" not in flat:
            flat["duration"] = flat.pop("seconds")
        ref = flat.pop("input_reference", None)
        if ref and "image" not in flat:
            flat["image"] = ref if str(ref).startswith("data:") else "data:image/png;base64," + str(ref)
        for num in ("duration", "timeout"):
            if isinstance(flat.get(num), str):
                try:
                    flat[num] = int(float(flat[num]))
                except ValueError:
                    flat.pop(num)
        body = flat
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "请求体不是合法 JSON，也不是 multipart/form-data") from None
    if not isinstance(body, dict) or not str(body.get("prompt") or "").strip():
        raise HTTPException(400, "缺少 prompt")
    try:
        return VideoRequest(**body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"参数不合法: {exc}") from None


@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def create_video_route(request: Request, _=Depends(auth)):
    return await create_video(await _video_request_from(request))


async def create_video(req: VideoRequest):
    ref_images = await asyncio.to_thread(
''',
)
patch(
    "app.py",
    "    threading.Thread(target=worker, daemon=True).start()\n"
    "    return {\"id\": task[\"id\"], \"task_id\": task[\"id\"], \"object\": \"video.task\", \"status\": \"queued\",\n"
    "            \"progress\": 10, \"created_at\": task[\"created_at\"]}\n",
    "    threading.Thread(target=worker, daemon=True).start()\n"
    "    return _video_openai_view({**store.get_task(task[\"id\"]), \"task_id\": task[\"id\"],\n"
    "                               \"model\": resolve_model(req.model, default=\"muse-video\"),\n"
    "                               \"seconds\": str(req.duration or 6), \"progress\": 10})\n",
)
patch(
    "app.py",
    "@app.get(\"/v1/videos/{task_id}\")\n"
    "@app.get(\"/v1/videos/generations/{task_id}\")\n"
    "def get_video(task_id: str, _=Depends(auth)):\n"
    "    t = store.get_task(task_id)\n"
    "    if not t:\n"
    "        raise HTTPException(404, \"task 不存在\")\n"
    "    out = dict(t)\n"
    "    status = out.get(\"status\")\n"
    "    if status in (\"succeeded\", \"success\", \"done\"):\n"
    "        out[\"status\"] = \"completed\"\n"
    "    if out.get(\"status\") == \"completed\":\n"
    "        out[\"progress\"] = 100\n"
    "    elif out.get(\"status\") == \"processing\":\n"
    "        # Progress reports observed milestones only; Muse exposes no percentage.\n"
    "        out[\"progress\"] = max(out.get(\"progress\", 0) or 0, 15)\n"
    "    vurl = out.get(\"url\")\n"
    "    if not vurl and isinstance(out.get(\"result\"), dict):\n"
    "        vurl = out[\"result\"].get(\"url\")\n"
    "    if vurl:\n"
    "        out[\"url\"] = vurl\n"
    "        if \"video\" not in out:\n"
    "            out[\"video\"] = {\"url\": vurl}\n"
    "    return out\n",
    "@app.get(\"/v1/videos/{task_id}\")\n"
    "@app.get(\"/v1/videos/generations/{task_id}\")\n"
    "def get_video(task_id: str, _=Depends(auth)):\n"
    "    t = store.get_task(task_id)\n"
    "    if not t:\n"
    "        raise HTTPException(404, \"task 不存在\")\n"
    "    return _video_openai_view({**t, \"task_id\": t[\"id\"]})\n"
    "\n"
    "\n"
    "@app.get(\"/v1/videos/{task_id}/content\")\n"
    "def get_video_content(task_id: str, variant: str | None = None, _=Depends(auth)):\n"
    "    \"\"\"The OpenAI flow downloads the mp4 from here, so LiteLLM clients never need\n"
    "    MUSE2API_PUBLIC_BASE to be reachable from wherever they run.\"\"\"\n"
    "    t = store.get_task(task_id)\n"
    "    if not t:\n"
    "        raise HTTPException(404, \"task 不存在\")\n"
    "    res = t.get(\"result\") if isinstance(t.get(\"result\"), dict) else {}\n"
    "    name = res.get(\"filename\") or \"\"\n"
    "    if not name:\n"
    "        raise HTTPException(409, f\"视频还没生成完（status={t.get('status')}）\")\n"
    "    if \"/\" in name or \"\\\\\" in name or \"..\" in name:\n"
    "        raise HTTPException(400, \"非法文件名\")\n"
    "    path = os.path.join(CFG.media_dir, name)\n"
    "    if not os.path.isfile(path):\n"
    "        raise HTTPException(404, \"成品文件已不在本地（可能已被清理）\")\n"
    "    kind = (res.get(\"kind\") or \"\").lower()\n"
    "    mt = \"video/mp4\" if \"mp4\" in kind or name.endswith(\".mp4\") else \\\n"
    "        (mimetypes.guess_type(name)[0] or \"application/octet-stream\")\n"
    "    return FileResponse(path, media_type=mt, filename=name)\n",
)
# app.py has no `mimetypes` / `Request` import of its own for the two uses above
# (`Request` is already imported from fastapi by upstream; mimetypes is not).
patch(
    "app.py",
    "import threading\nimport time\nimport uuid\nimport zipfile\n",
    "import mimetypes\nimport threading\nimport time\nimport uuid\nimport zipfile\n",
)

# 11. Spread videos across accounts instead of piling them on one (2026-10-30).
#     Upstream prefers "the last account that completed a video". With patch #8
#     (5 slots per account) that account always has a free slot, so every video
#     lands on it: 10-30 e2e put 5/5 videos (3 concurrent) on muse-gen-12. Two costs:
#     concurrent videos slow down on one account (71~77s alone -> 111~121s three at
#     once), and only that one account's weekly video quota gets spent.
#     Fix: keep the preference only while that account has nothing in flight
#     (ACCOUNT_POOL.leases counts every task kind, not just video); once it is busy,
#     let pick_account() rotate to the least recently used healthy account.
#     Risk this accepts: upstream sticks because "session health alone is
#     insufficient" -- some accounts may not be able to make video. Verified on
#     rollout with concurrent videos that each landed on a different account.
#     MUSE2API_VIDEO_STICKY=1 restores upstream behaviour.
patch(
    "app.py",
    "            candidate = store.get_account(task[\"account\"])\n"
    "            if candidate and candidate.get(\"enabled\", True) and candidate.get(\"ok\") is not False:\n"
    "                preferred_id = candidate[\"id\"]\n"
    "                break\n",
    "            candidate = store.get_account(task[\"account\"])\n"
    "            if candidate and candidate.get(\"enabled\", True) and candidate.get(\"ok\") is not False:\n"
    "                # harden #11: only stick while it has no video in flight; otherwise rotate.\n"
    "                if (os.environ.get(\"MUSE2API_VIDEO_STICKY\") != \"1\"\n"
    "                        and ACCOUNT_POOL.leases.get(candidate[\"id\"], 0) > 0):\n"
    "                    preferred_id = None\n"
    "                    break\n"
    "                preferred_id = candidate[\"id\"]\n"
    "                break\n",
)
