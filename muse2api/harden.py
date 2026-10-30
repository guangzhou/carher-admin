#!/usr/bin/env python3
"""Harden a pinned muse2api checkout before building it.

Each edit must match exactly once, otherwise the script fails, so an upstream
change can't silently skip a patch.

1. /admin/update/* and /admin/repo/* return 404 (no pulling code from GitHub, no git push).
2. An auto-generated API key is no longer logged in plain text.
3. Reference images: pin the two upstream safety switches to off, so a stray env
   var can't re-enable local-file reads or private-network fetches (SSRF).
   Upstream v1.6.4 (PR #24) implements the check itself -- with DNS-rebinding and
   redirect guards our old patch never had -- so we no longer rewrite that code.
4. /v1/chat/completions with model=muse-image runs the image pipeline and replies
   with a markdown image (upstream sends it to the text assistant -> 502 after 40s).
5. Account picking skips accounts whose session is known dead (ok=False after a 401);
   upstream falls back to them whenever the healthy one is busy -> instant 401.
6. Optional egress proxy (MUSE2API_EGRESS_PROXY) for Chromium; requests uses ALL_PROXY + PySocks.
7. Per-account egress: each account can pin its own exit IP (MUSE2API_EGRESS_MAP).
   Ported from sunxu/muse2api@feature/account-proxy-isolation.
"""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1])
HERE = pathlib.Path(__file__).resolve().parent


def patch(rel, old, new):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n != 1:
        sys.exit(f"[harden] {rel}: expected 1 match, got {n}: {old[:60]!r}")
    p.write_text(text.replace(old, new), encoding="utf-8")
    print(f"[harden] {rel}: ok")


def add_file(rel, src):
    """Copy one vendored module into the checkout; refuse to clobber upstream."""
    dst = ROOT / rel
    if dst.exists():
        sys.exit(f"[harden] {rel}: already exists upstream, resolve by hand")
    dst.write_bytes((HERE / src).read_bytes())
    print(f"[harden] {rel}: added")


# 1. Block the self-update / repo-push endpoints.
patch(
    "app.py",
    "store = Store(CFG)\nengine = MuseEngine(CFG)\n",
    "store = Store(CFG)\nengine = MuseEngine(CFG)\n"
    "\n"
    "\n"
    "@app.middleware(\"http\")\n"
    "async def _block_self_update(request: Request, call_next):\n"
    "    # Pinned deployment: never pull code from GitHub or push the repo.\n"
    "    if request.url.path.startswith((\"/admin/update\", \"/admin/repo\")):\n"
    "        return JSONResponse({\"detail\": \"disabled in this deployment\"}, status_code=404)\n"
    "    return await call_next(request)\n",
)

# 2. Don't log an auto-generated key.
patch(
    "app.py",
    'log.info("🔑 未检测到 MUSE2API_KEY，已自动生成初始密钥: %s", new_key)',
    'log.info("🔑 未检测到 MUSE2API_KEY，已自动生成初始密钥（不打印，见 /admin/apikey）")',
)

# 3. Reference images: pin both escape hatches shut (2026-10-29).
#    v1.6.4 does the SSRF check natively (engine._open_public_ref_image: public-IP
#    only, re-resolves per redirect, no redirect handler), gated on these two env
#    vars which default to "0". We hard-pin them to False so no env var, .env file
#    or future default flip can turn server-side file reads or private fetches on.
patch(
    "config.py",
    "    ref_image_local_files: bool = field(\n"
    "        default_factory=lambda: _env(\"MUSE2API_REF_IMAGE_LOCAL_FILES\", \"0\").strip() == \"1\")\n"
    "    ref_image_private_fetch: bool = field(\n"
    "        default_factory=lambda: _env(\"MUSE2API_REF_IMAGE_PRIVATE_FETCH\", \"0\").strip() == \"1\")\n",
    "    # harden: pinned off. Shared deployment behind LiteLLM -- callers are not\n"
    "    # trusted with the pod filesystem or the cluster network.\n"
    "    ref_image_local_files: bool = False\n"
    "    ref_image_private_fetch: bool = False\n",
)

# 4. Chat with model=muse-image generates an image (2026-10-16).
#    Upstream sends it to the text assistant, which never produces media and
#    502s after 40s ("等待助手首字响应超时"). Cursor/Cherry/OWUI only speak chat,
#    so route the last user turn through the image pipeline and answer with a
#    markdown image whose URL comes from media_url() (absolute when PUBLIC_BASE is set).
patch(
    "app.py",
    "    prompt = build_chat_prompt(req.messages) or (req.prompt or \"\").strip()\n"
    "    if not prompt:\n"
    "        raise HTTPException(400, \"messages 为空\")\n",
    "    prompt = build_chat_prompt(req.messages) or (req.prompt or \"\").strip()\n"
    "    if not prompt:\n"
    "        raise HTTPException(400, \"messages 为空\")\n"
    "    if resolve_model(req.model, default=\"muse-spark\") == \"muse-image\":\n"
    "        return await _chat_as_image(req)\n",
)
patch(
    "app.py",
    "@app.post(\"/v1/chat/completions\")\n",
    "async def _chat_as_image(req: \"ChatRequest\"):\n"
    "    last = next((m for m in reversed(req.messages) if (m.role or \"\").lower() == \"user\"), None)\n"
    "    text = _content_text(last.content).strip() if last else (req.prompt or \"\").strip()\n"
    "    if not text:\n"
    "        raise HTTPException(400, \"messages 里没有用户描述\")\n"
    "    gen_prompt = build_image_prompt(ImageRequest(prompt=text))\n"
    "    cid, created = \"chatcmpl-\" + uuid.uuid4().hex[:24], int(time.time())\n"
    "    usage = {\"prompt_tokens\": 0, \"completion_tokens\": 0, \"total_tokens\": 0}\n"
    "    if req.stream:\n"
    "        # Generation takes 30-200s; send the role chunk now and SSE comments while\n"
    "        # waiting so clients and proxies don't drop an idle stream.\n"
    "        def gen():\n"
    "            yield _sse(_chat_chunk(cid, created, \"muse-image\", {\"role\": \"assistant\", \"content\": \"\"}))\n"
    "            box = {}\n"
    "            def work():\n"
    "                try:\n"
    "                    box[\"res\"] = _run_generation(gen_prompt, \"image\", CFG.image_timeout)[0]\n"
    "                except Exception as exc:  # noqa: BLE001\n"
    "                    box[\"err\"] = exc\n"
    "            th = threading.Thread(target=work, daemon=True)\n"
    "            th.start()\n"
    "            while th.is_alive():\n"
    "                th.join(10)\n"
    "                if th.is_alive():\n"
    "                    yield \": generating\\n\\n\"\n"
    "            if \"err\" in box:\n"
    "                exc = box[\"err\"]\n"
    "                code = 401 if isinstance(exc, MuseAuthError) else 502\n"
    "                yield _sse({\"error\": {\"message\": str(exc), \"type\": \"server_error\", \"code\": code}})\n"
    "                return\n"
    "            content = f\"![image]({media_url(box['res']['filename'])})\"\n"
    "            yield _sse(_chat_chunk(cid, created, \"muse-image\", {\"content\": content}))\n"
    "            yield _sse(_chat_chunk(cid, created, \"muse-image\", {}, finish=\"stop\"))\n"
    "            if _want_usage(req.stream_options):\n"
    "                yield _sse({\"id\": cid, \"object\": \"chat.completion.chunk\", \"created\": created,\n"
    "                            \"model\": \"muse-image\", \"choices\": [], \"usage\": usage})\n"
    "            yield \"data: [DONE]\\n\\n\"\n"
    "        return StreamingResponse(gen(), media_type=\"text/event-stream\", headers=_SSE_HEADERS)\n"
    "    try:\n"
    "        res, _acc = await asyncio.to_thread(_run_generation, gen_prompt, \"image\", CFG.image_timeout)\n"
    "    except MuseAuthError as exc:\n"
    "        raise HTTPException(401, str(exc)) from exc\n"
    "    except MuseGenerationError as exc:\n"
    "        raise HTTPException(502, str(exc)) from exc\n"
    "    content = f\"![image]({media_url(res['filename'])})\"\n"
    "    return {\"id\": cid, \"object\": \"chat.completion\", \"created\": created, \"model\": \"muse-image\",\n"
    "            \"choices\": [{\"index\": 0, \"finish_reason\": \"stop\",\n"
    "                         \"message\": {\"role\": \"assistant\", \"content\": content}}],\n"
    "            \"usage\": usage}\n"
    "\n"
    "\n"
    "@app.post(\"/v1/chat/completions\")\n",
)

# 5. Never hand out an account whose session is known dead (2026-10-16).
#    pick_account() keeps ok=False accounts in `live`; when the only healthy
#    account is busy (exclude_ids), it returns the dead one and the request
#    fails at once with /api/session 401 -- 3 user requests in one morning.
#    Waiting for the healthy account is always better than a guaranteed 401.
#    ok=None (unconfirmed) stays eligible; a successful keepalive/test sets ok=True.
patch(
    "store.py",
    "            live = [a for a in self.accounts if a.get(\"enabled\", True) and a.get(\"cookies\")]\n"
    "            if not live:\n"
    "                return None\n"
    "            blocked = set(exclude_ids or [])",
    "            live = [a for a in self.accounts if a.get(\"enabled\", True) and a.get(\"cookies\")\n"
    "                    and a.get(\"ok\") is not False]\n"
    "            if not live:\n"
    "                return None\n"
    "            blocked = set(exclude_ids or [])",
)

# 6. Default egress proxy (2026-10-16): send muse.ai traffic through
#    MUSE2API_EGRESS_PROXY (socks5h://127.0.0.1:1080 from the ssh sidecar ->
#    Aliyun Singapore 47.84.85.100). Three of four accounts were logged out in one
#    night on the shared Japan exit. Chromium gets --proxy-server (local CDP on
#    127.0.0.1 bypassed); `requests` reads ALL_PROXY/HTTPS_PROXY from the pod env
#    and needs PySocks for socks5h. Empty env = direct, i.e. old behaviour.
#    Patch 7 overrides this per account for accounts listed in MUSE2API_EGRESS_MAP.
patch(
    "engine.py",
    "            \"--remote-allow-origins=*\",\n",
    "            \"--remote-allow-origins=*\",\n"
    "            *([f\"--proxy-server={os.environ['MUSE2API_EGRESS_PROXY'].replace('socks5h://', 'socks5://')}\",\n"
    "               \"--proxy-bypass-list=127.0.0.1;localhost\"]\n"
    "              if os.environ.get(\"MUSE2API_EGRESS_PROXY\") else []),\n",
)
patch(
    "requirements.txt",
    "requests>=2.32\n",
    "requests[socks]>=2.32\n",
)

# 7. Per-account egress IP (2026-10-29).
#    Meta ties accounts together by exit IP: all accounts sharing one IP look like
#    one actor. Ported from sunxu/muse2api@feature/account-proxy-isolation, with the
#    account -> proxy mapping coming from env (MUSE2API_EGRESS_MAP) instead of a new
#    admin field, so no account record or admin UI changes are needed.
#
#    MUSE2API_EGRESS_MAP = "<label-or-id>=<proxy-url>,<label-or-id>=<proxy-url>"
#    e.g. "muse-horwich-04=http://kiro:PW@10.43.25.225:7890"
#    An account not in the map keeps the pod-wide MUSE2API_EGRESS_PROXY tunnel (#6).
#
#    Why the local bridge: Chromium's CDP proxyServer takes no credentials, and the
#    cluster mihomo proxies answer 407 without HTTP Basic. proxy_bridge.py listens on
#    a loopback port per account and adds the Proxy-Authorization upstream, so the
#    credential never reaches Chromium's command line or a CDP message.
#
#    Hard rules (all from sunxu, all deliberate):
#      - a proxy that fails is an error, never a silent fall back to the pod IP;
#      - the page cache is keyed on the proxy too, so changing a mapping rebuilds
#        the context instead of reusing one on the old exit;
#      - renew_session_http / VM wake use the same per-account proxy as the browser,
#        otherwise /api/session is seen from a different IP than the chat WebSocket;
#      - a bad MUSE2API_EGRESS_MAP raises MuseGenerationError, never MuseAuthError:
#        our own misconfiguration must not mark a live account's cookie dead.
add_file("proxy_bridge.py", "proxy_bridge.py")

patch(
    "Dockerfile",
    "COPY admin.html app.py cdp.py config.py engine.py store.py version.json ./\n",
    "COPY admin.html app.py cdp.py config.py engine.py store.py version.json ./\n"
    "COPY proxy_bridge.py ./\n",
)

patch(
    "engine.py",
    "        self._last_http_renew: dict[str, float] = {}\n"
    "        self._log = None\n",
    "        self._last_http_renew: dict[str, float] = {}\n"
    "        self._log = None\n"
    "        from proxy_bridge import ProxyBridgeManager, parse_egress_map\n"
    "        self.proxy_bridges = ProxyBridgeManager()\n"
    "        # label/id -> upstream proxy URL. Parsed once; a malformed entry fails here.\n"
    "        self.egress_map = parse_egress_map(os.environ.get(\"MUSE2API_EGRESS_MAP\", \"\"))\n"
    "        self.account_label = None  # app.py binds account id -> label\n",
)

patch(
    "engine.py",
    "    @current_context_id.setter\n"
    "    def current_context_id(self, val: str | None):\n"
    "        self._ensure_local().current_context_id = val\n",
    "    @current_context_id.setter\n"
    "    def current_context_id(self, val: str | None):\n"
    "        self._ensure_local().current_context_id = val\n"
    "\n"
    "    @property\n"
    "    def current_proxy_url(self) -> str | None:\n"
    "        return getattr(self._ensure_local(), \"current_proxy_url\", None)\n"
    "\n"
    "    @current_proxy_url.setter\n"
    "    def current_proxy_url(self, value: str | None):\n"
    "        self._ensure_local().current_proxy_url = value\n"
    "\n"
    "    def proxy_for_account(self, account_id: str | None) -> tuple[str, str]:\n"
    "        \"\"\"(upstream proxy, loopback bridge) for one account; ('', '') = pod default.\n"
    "\n"
    "        Looked up by account id first, then by label, so a re-imported account\n"
    "        keeps its exit as long as the label is reused.\n"
    "        \"\"\"\n"
    "        if not account_id or not self.egress_map:\n"
    "            return \"\", \"\"\n"
    "        configured = self.egress_map.get(account_id)\n"
    "        if not configured and self.account_label:\n"
    "            label = self.account_label(account_id)\n"
    "            if label:\n"
    "                configured = self.egress_map.get(label)\n"
    "        if not configured:\n"
    "            return \"\", \"\"\n"
    "        return configured, self.proxy_bridges.local_url(account_id, configured)\n",
)

# 7b. _open_page takes the per-account proxy. Upstream falls back to a plain tab
#     (the shared browser context, i.e. the pod IP) when createBrowserContext
#     fails; with a pinned exit that fallback would leak the account onto the wrong
#     IP, so when a proxy is requested the failure is raised instead.
patch(
    "engine.py",
    "    def _open_page(self):\n"
    "        \"\"\"为当前工作线程创建独立的页面。\n",
    "    def _open_page(self, proxy_server: str = \"\"):\n"
    "        \"\"\"为当前工作线程创建独立的页面。\n",
)
patch(
    "engine.py",
    "        if self.browser:\n"
    "            try:\n"
    "                res = self.browser.send(\"Target.createBrowserContext\")\n",
    "        if self.browser:\n"
    "            try:\n"
    "                res = self.browser.send(\n"
    "                    \"Target.createBrowserContext\",\n"
    "                    {\"proxyServer\": proxy_server,\n"
    "                     \"proxyBypassList\": \"127.0.0.1;localhost\"} if proxy_server else {})\n",
)
patch(
    "engine.py",
    "            except Exception as exc:\n"
    "                log.warning(\"Target.createBrowserContext 创建隔离上下文失败，回退到普通标签页: %s\", exc)\n"
    "\n"
    "        import requests\n",
    "            except Exception as exc:\n"
    "                if proxy_server:\n"
    "                    # Pinned exit: a plain tab would use the pod IP. Fail instead.\n"
    "                    raise MuseGenerationError(\n"
    "                        f\"创建带固定出口的隔离上下文失败，拒绝回退到共享出口: {type(exc).__name__}\") from None\n"
    "                log.warning(\"Target.createBrowserContext 创建隔离上下文失败，回退到普通标签页: %s\", exc)\n"
    "        if proxy_server:\n"
    "            raise MuseGenerationError(\"浏览器 CDP 未连接或未返回隔离上下文，拒绝使用共享出口\")\n"
    "\n"
    "        import requests\n",
)

# 7c. /api/session renewal and the VM wake must leave from the same IP as the chat
#     WebSocket. Without this the cookie is refreshed from the pod's default exit
#     while the browser talks from the pinned one -- two IPs per account per turn,
#     which is the pattern we are trying to remove.
patch(
    "engine.py",
    "    def renew_session_http(cookies: dict, expires: dict | None = None,\n"
    "                           wake_vm: bool = True) -> dict:",
    "    def renew_session_http(cookies: dict, expires: dict | None = None,\n"
    "                           wake_vm: bool = True, proxy_server: str = \"\") -> dict:",
)
patch(
    "engine.py",
    "        import requests\n"
    "        cur_cookies = dict(cookies or {})\n",
    "        import requests\n"
    "        # Explicit per-account proxy wins over ALL_PROXY from the pod env.\n"
    "        proxy_kwargs = ({\"proxies\": {\"http\": proxy_server, \"https\": proxy_server}}\n"
    "                        if proxy_server else {})\n"
    "        cur_cookies = dict(cookies or {})\n",
)
patch(
    "engine.py",
    "            r = requests.get(\"https://muse.ai/api/session\", headers=headers,\n"
    "                             timeout=12, allow_redirects=False)",
    "            r = requests.get(\"https://muse.ai/api/session\", headers=headers,\n"
    "                             timeout=12, allow_redirects=False, **proxy_kwargs)",
)
patch(
    "engine.py",
    "                }, timeout=8)\n",
    "                }, timeout=8, **proxy_kwargs)\n",
)
patch(
    "engine.py",
    "                    verify = requests.get(\"https://muse.ai/api/session\", headers=headers,\n"
    "                                          timeout=12, allow_redirects=False)",
    "                    verify = requests.get(\"https://muse.ai/api/session\", headers=headers,\n"
    "                                          timeout=12, allow_redirects=False, **proxy_kwargs)",
)

# 7d. ensure_page: resolve the account's exit, key the page cache on it, and pass
#     it to both the renewal and the browser context.
patch(
    "engine.py",
    "        if self.page is not None and (account_id is None or getattr(self, \"current_acc_id\", None) == account_id):",
    "        configured_proxy, proxy_server = self.proxy_for_account(account_id)\n"
    "        # Reusing a page whose context has a different exit would silently move\n"
    "        # the account to another IP, so the proxy is part of the cache key.\n"
    "        if (self.page is not None and self.current_proxy_url == configured_proxy\n"
    "                and (account_id is None or getattr(self, \"current_acc_id\", None) == account_id)):",
)
patch(
    "engine.py",
    "                renewed = self.renew_session_http(cookies, expires, wake_vm=True)",
    "                renewed = self.renew_session_http(cookies, expires, wake_vm=True,\n"
    "                                                  proxy_server=proxy_server)",
)
patch(
    "engine.py",
    "        page = self._open_page()\n"
    "        self._apply_cookies(page, cookies, expires)",
    "        page = self._open_page(proxy_server=proxy_server)\n"
    "        self._apply_cookies(page, cookies, expires)",
)
patch(
    "engine.py",
    "        if self._wait_ws_ready(page, timeout=self.cfg.login_wait):\n"
    "            self.current_acc_id = account_id\n"
    "            return page",
    "        if self._wait_ws_ready(page, timeout=self.cfg.login_wait):\n"
    "            self.current_acc_id = account_id\n"
    "            self.current_proxy_url = configured_proxy\n"
    "            return page",
)
patch(
    "engine.py",
    "            self.current_context_id = None\n"
    "        self.current_acc_id = None\n",
    "            self.current_context_id = None\n"
    "        self.current_acc_id = None\n"
    "        self.current_proxy_url = None\n",
)

# 7e. app.py: let the engine resolve an account id to its label, so the map can be
#     written with stable labels (muse-horwich-04) instead of random hex ids.
patch(
    "app.py",
    "store = Store(CFG)\nengine = MuseEngine(CFG)\n",
    "store = Store(CFG)\nengine = MuseEngine(CFG)\n"
    "engine.account_label = lambda aid: (store.get_account(aid) or {}).get(\"label\", \"\")\n",
)

# 7f. app.py's own two renewal paths must use the account's exit too (2026-10-30).
#     7d only fixed the renewal *inside* engine.ensure_page. app.py calls
#     renew_session_http twice more, both without proxy_server:
#       - _renew_and_persist (app.py:321)  = pre-request re-sign, every request
#       - _probe_account_sync (app.py:2439) = the 15-minute keepalive loop
#     Measured in the live pod 2026-10-30: `requests` with no `proxies=` follows
#     ALL_PROXY=socks5h://127.0.0.1:1080 and comes out of 47.84.85.100, i.e. every
#     account's /api/session + VM wake was seen from the shared default tunnel while
#     its chat WebSocket came from its own IP. That is exactly the cookie-from-A /
#     chat-from-B correlation per-account egress exists to remove.
#     engine.proxy_for_account returns ("", "") for an account not in the map, so an
#     unmapped account keeps the old behaviour (pod-level tunnel).
patch(
    "app.py",
    "        res = engine.renew_session_http(acc[\"cookies\"], acc.get(\"cookies_exp\"), wake_vm=wake_vm)\n",
    "        _, _bridge = engine.proxy_for_account(acc_id)\n"
    "        res = engine.renew_session_http(acc[\"cookies\"], acc.get(\"cookies_exp\"), wake_vm=wake_vm,\n"
    "                                        proxy_server=_bridge)\n",
)
patch(
    "app.py",
    "        res = engine.renew_session_http(acc[\"cookies\"], acc.get(\"cookies_exp\"), wake_vm=True)\n",
    "        _, _bridge = engine.proxy_for_account(aid)\n"
    "        res = engine.renew_session_http(acc[\"cookies\"], acc.get(\"cookies_exp\"), wake_vm=True,\n"
    "                                        proxy_server=_bridge)\n",
)

# 8. Several tasks at once on the same account (2026-10-29).
#    Upstream allows one task per account (busy_accounts set), so with one live
#    account 5 videos run strictly one after another (90/221/301/371/491s).
#    In-pod test on muse-horwich-04, each task in its own BrowserContext/thread:
#    N=2 2/2 ok in 112s; N=5 5/5 ok in 167s. Two failure shapes seen and fixed below:
#    - 2 of 5 tabs hit the 60s page-load wait while 5 pages opened at once
#      -> MUSE2API_LOGIN_WAIT=150 in k8s.yaml (env already exists upstream).
#    - 3 of 5 tabs waited the full 600s, yet a reload of the same thread showed the
#      finished <video>: the result was made, only that tab missed the push (debug
#      dump: video card present, src ""). 8b reloads the thread instead of waiting.
#    MUSE2API_PER_ACCOUNT_SLOTS (default 1 = upstream behaviour) caps tasks per account.
patch(
    "app.py",
    "        self.busy_accounts: set[str] = set()\n"
    "        self.cond = threading.Condition()\n",
    "        self.busy_accounts: set[str] = set()  # accounts with every slot taken\n"
    "        self.leases: dict[str, int] = {}\n"
    "        self.slots = max(1, int(os.environ.get(\"MUSE2API_PER_ACCOUNT_SLOTS\", \"1\") or 1))\n"
    "        self.cond = threading.Condition()\n"
    "\n"
    "    def _lease(self, account_id: str):\n"
    "        n = self.leases.get(account_id, 0) + 1\n"
    "        self.leases[account_id] = n\n"
    "        if n >= self.slots:\n"
    "            self.busy_accounts.add(account_id)\n",
)
patch(
    "app.py",
    "                    if account_id not in self.busy_accounts:\n"
    "                        self.busy_accounts.add(account_id)\n"
    "                        return acc\n",
    "                    if account_id not in self.busy_accounts:\n"
    "                        self._lease(account_id)\n"
    "                        return acc\n",
)
patch(
    "app.py",
    "                    if acc:\n"
    "                        self.busy_accounts.add(acc[\"id\"])\n"
    "                        return acc\n",
    "                    if acc:\n"
    "                        self._lease(acc[\"id\"])\n"
    "                        return acc\n",
)
patch(
    "app.py",
    "        with self.cond:\n"
    "            self.busy_accounts.discard(account_id)\n"
    "            self.cond.notify_all()\n",
    "        with self.cond:\n"
    "            n = self.leases.get(account_id, 0) - 1\n"
    "            if n > 0:\n"
    "                self.leases[account_id] = n\n"
    "            else:\n"
    "                self.leases.pop(account_id, None)\n"
    "            self.busy_accounts.discard(account_id)\n"
    "            self.cond.notify_all()\n",
)

# 8b. Video wait: if the thread exists but no video source showed up, reload that
#     thread (at 200s, then every 100s, at most 4 times) and keep polling.
patch(
    "engine.py",
    "        seen_stop = False\n"
    "        previous_state = None\n"
    "        last_activity = t_start\n",
    "        seen_stop = False\n"
    "        previous_state = None\n"
    "        last_activity = t_start\n"
    "        reloads, last_reload = 0, t_start\n",
)
patch(
    "engine.py",
    "            # Muse accepted the click but never created a thread. Requiring a completed\n",
    "            if (expect == \"video\" and elapsed >= 200.0 and reloads < 4\n"
    "                    and time.time() - last_reload >= 100.0\n"
    "                    and \"/thread/\" in path and not path.rstrip(\"/\").endswith(\"/thread/new\")):\n"
    "                reloads, last_reload = reloads + 1, time.time()\n"
    "                log.info(\"video tab got no media after %ds, reloading thread (%d/4)\", int(elapsed), reloads)\n"
    "                try:\n"
    "                    self.page.send(\"Page.navigate\", {\"url\": st.get(\"href\") or path})\n"
    "                    time.sleep(1.0)\n"
    "                    self._wait_ws_ready(self.page, timeout=30.0)\n"
    "                except Exception:\n"
    "                    pass\n"
    "                continue\n"
    "            # Muse accepted the click but never created a thread. Requiring a completed\n",
)
