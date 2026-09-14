# sitecustomize.py — auto-imported by CPython's ``site`` at EVERY interpreter
# startup. Mounted into the venv site-packages
# (/app/.venv/lib/python3.13/site-packages/sitecustomize.py) so it loads in the
# gunicorn master AND in every ``--num_workers`` spawned worker, BEFORE any
# request handler or the spend-log flush background job runs.
#
# WHY THIS EXISTS  (root-caused 2026-07-23, LiteLLM 1.90.2 prod on 198)
# --------------------------------------------------------------------
# ``null_byte_sanitize.py`` monkey-patches ``PrismaClient.jsonify_object`` to
# scrub NUL bytes / lone UTF-16 surrogates out of spend-log rows before the
# batch flush (``litellm/proxy/utils.py::update_spend_logs`` ->
# ``SpendLogsRepository(...).table.create_many``). That patch is loaded via
# ``litellm_settings.callbacks``, which LiteLLM imports LAZILY and PER-WORKER.
#
# With ``--num_workers 2`` the worker that runs the ``update_spend_logs``
# background job can start flushing BEFORE it has imported the callback module,
# so it runs the UNPATCHED ``jsonify_object``. A single row carrying a raw NUL byte
# or half a truncated emoji (extremely common in codex/cursor code + terminal
# traffic, especially with ``store_prompts_in_spend_logs: true``) then makes
# PostgreSQL/prisma reject the ENTIRE up-to-1000-row ``create_many`` batch
# (SQLSTATE 22P05 "unsupported Unicode escape" / "unexpected end of hex
# escape"), and LiteLLM's flush loop drops the whole batch with no retry and no
# single-row fallback. Measured ~55 such failures/hour in prod; the result is
# that /spend/logs, the UI "Logs" tab and "Session Logs" are effectively empty
# while aggregate spend (key/user/team + Daily* tables) keeps updating — two
# independent write paths.
#
# The fix below re-applies the same scrub, but DETERMINISTICALLY in every
# interpreter, DEFERRED via a ``sys.meta_path`` finder that patches
# ``litellm.proxy.utils`` the instant it is first imported (by whichever code
# imports it first, INCLUDING the flush worker). It never imports litellm
# eagerly (no startup/circular-import risk) and is fully fail-open: any error
# here is swallowed so it can never block interpreter boot.
#
# CONTENT RETENTION  (changed 2026-08-15)
# ---------------------------------------
# Originally (2026-07-23, "option B") this hook ALSO blanked
# ``messages``/``response``/``proxy_server_request`` on every spend-log row, i.e.
# it behaved like ``store_prompts_in_spend_logs=false`` enforced at the last
# step before create_many. That made the UI "Logs" input/output columns empty
# for ALL keys (verified 2026-08-15: last 3000 rows had messages == response ==
# "{}"), defeating ``store_prompts_in_spend_logs: true``. Per request to log
# cursor-key (and all-key) input/output, the content-drop is REMOVED. The
# deterministic ``_scrub`` below (NUL / lone-surrogate removal) is retained and
# is what actually prevents the 22P05 batch-poison — dropping content was a
# redundant belt-and-suspenders, not the real guard. Row content now lands
# intact after scrubbing.
#
# Diagnosis proof: utils.py:5591 still calls ``prisma_client.jsonify_object``
# (so the hook point is correct on 1.90.2), and manual ``import
# null_byte_sanitize`` in-pod scrubs ``a<NUL>b`` -> ``ab`` correctly — the
# only gap was that the running flush worker had not applied the patch.
#
# Remove once upstream LiteLLM ships its own sanitization (BerriAI/litellm
# #21290 / #24310 / #19847) or the callback import is made eager per-worker.

import sys

# Callback modules are mounted under /app while sitecustomize itself is loaded
# from the virtualenv site-packages directory. Put the mount on sys.path before
# importing sibling callbacks so startup behavior is deterministic in every
# worker process.
_APP_DIR = "/app"
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

# Preserve the prior sitecustomize behavior. Guarded: if responses_aclose is
# not on the path in this deployment it simply logs and continues.
try:
    import responses_aclose  # noqa: F401
except Exception as _exc:  # pragma: no cover
    print("[sitecustomize] responses_aclose import failed: " + repr(_exc), file=sys.stderr)


def _install_spendlog_sanitizer():
    import re
    from importlib.abc import MetaPathFinder
    from importlib.util import find_spec

    _TARGET = "litellm.proxy.utils"

    # ── scrub logic (mirrors null_byte_sanitize.py) ──────────────────────
    _NUL_RAW = "\x00"
    _NUL_ESCAPED = "\\u0000"
    _LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
    # literal \uD800..\uDBFF NOT followed by a low surrogate (unpaired high)
    _LITERAL_HIGH_LONE_RE = re.compile(
        r"\\u[dD][89aAbB][0-9a-fA-F]{2}(?!\\u[dD][c-fC-F][0-9a-fA-F]{2})"
    )
    # literal \uDC00..\uDFFF NOT preceded by a high surrogate (unpaired low)
    _LITERAL_LOW_LONE_RE = re.compile(
        r"(?<!\\u[dD][89aAbB][0-9a-fA-F]{2})\\u[dD][c-fC-F][0-9a-fA-F]{2}"
    )

    def _scrub_str(s):
        if _NUL_RAW in s or _NUL_ESCAPED in s:
            s = s.replace(_NUL_RAW, "").replace(_NUL_ESCAPED, "")
        if _LONE_SURROGATE_RE.search(s):
            s = _LONE_SURROGATE_RE.sub("", s)
        if "\\u" in s:
            s = _LITERAL_HIGH_LONE_RE.sub("", s)
            s = _LITERAL_LOW_LONE_RE.sub("", s)
        return s

    def _scrub(v):
        if isinstance(v, str):
            return _scrub_str(v)
        if isinstance(v, dict):
            return {k: _scrub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_scrub(x) for x in v]
        if isinstance(v, tuple):
            return tuple(_scrub(x) for x in v)
        return v

    def _patch_utils(mod):
        try:
            PrismaClient = getattr(mod, "PrismaClient", None)
            if PrismaClient is None:
                return
            orig = PrismaClient.jsonify_object
            if getattr(orig, "_spendlog_sanitize_patched", False):
                return

            def patched_jsonify_object(self, data):
                # Scrub PG/prisma-poison bytes (NUL / lone UTF-16 surrogates)
                # out of the ENTIRE row so a single bad byte can never reject
                # the whole create_many batch. Content fields
                # (messages/response/proxy_server_request) are retained intact
                # after scrubbing so store_prompts_in_spend_logs actually
                # surfaces input/output in /spend/logs and the UI.
                try:
                    data = _scrub(data)
                except Exception:
                    pass  # fail-open: never block a spend-log write on the hook
                return orig(self, data)

            patched_jsonify_object._spendlog_sanitize_patched = True
            PrismaClient.jsonify_object = patched_jsonify_object
            print(
                "[sitecustomize] spendlog sanitizer patched "
                "PrismaClient.jsonify_object (scrubs \\x00 / \\u0000 / lone "
                "surrogates before create_many; CONTENT RETAINED "
                "messages/response/proxy_server_request)",
                file=sys.stderr,
            )
        except Exception as exc:  # pragma: no cover
            print("[sitecustomize] spendlog patch_utils failed: " + repr(exc), file=sys.stderr)

    # If litellm.proxy.utils is already imported, patch it right now.
    if _TARGET in sys.modules:
        _patch_utils(sys.modules[_TARGET])
        return

    # Otherwise install a one-shot post-import hook.
    class _Finder(MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != _TARGET:
                return None
            # Delegate to the real finders (with self removed to avoid
            # recursion), then wrap exec_module to patch once loaded.
            try:
                sys.meta_path.remove(self)
            except ValueError:
                pass
            try:
                spec = find_spec(name)
            finally:
                if self not in sys.meta_path:
                    sys.meta_path.insert(0, self)
            if spec is None or spec.loader is None:
                return None
            loader = spec.loader
            orig_exec = loader.exec_module

            def exec_module(module):
                orig_exec(module)          # real import first
                _patch_utils(module)       # then patch; never touches success path

            try:
                loader.exec_module = exec_module
            except Exception:
                # Some loaders forbid attribute assignment; fall back to a
                # deferred patch on next access via sys.modules check.
                orig_exec(spec.loader)  # best-effort; unlikely path
            return spec

    sys.meta_path.insert(0, _Finder())


try:
    _install_spendlog_sanitizer()
except Exception as _exc:  # pragma: no cover
    print("[sitecustomize] spendlog sanitizer install failed: " + repr(_exc), file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# UI SPEND-LOG RENDER FIX  (added 2026-08-16)
# ----------------------------------------------------------------------------
# The dashboard "Logs" drawer fetches /spend/logs/ui/{request_id} and renders:
#   * request pane:  parsed(proxy_server_request || messages).messages  — the
#     compiled UI has NO parser for the Responses-API "input" array (verified:
#     zero occurrences in every chunk under _experimental/out), so for
#     call_type=aresponses the Input pane is always empty even though
#     proxy_server_request holds the full input.
#   * response pane: renders output items of type "function_call" (and text
#     "message" items) but has ZERO handling for "custom_tool_call" (Codex
#     custom tools) — a response whose only output item is custom_tool_call
#     shows "No response data available".
#
# Fix at READ time (this hook), not write time: the endpoint below returns the
# stored row; we (a) inject a chat-shaped `messages` list derived from the
# Responses `instructions`/`input` into the returned proxy_server_request, and
# (b) present custom_tool_call output items as function_call-shaped so the
# existing renderer shows them. Stored rows are untouched (raw JSON view keeps
# original `input`; the derived `messages` key is additive) and the fix is
# retroactive for every already-logged row.
#
# Hook points: wrap the endpoint function on the spend_management_endpoints
# router route AND on the app route (include_router copies routes, so both are
# covered regardless of import order). Fail-open everywhere.

def _install_ui_spendlog_render_fix():
    import functools
    import json
    from importlib.abc import MetaPathFinder
    from importlib.util import find_spec

    _EP_MOD = "litellm.proxy.spend_tracking.spend_management_endpoints"
    _PS_MOD = "litellm.proxy.proxy_server"
    _ROUTE_PATH = "/spend/logs/ui/{request_id}"

    def _maybe_json(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return v
        return v

    def _content_to_str(c):
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for x in c:
                if isinstance(x, dict):
                    parts.append(x.get("text") or x.get("transcript") or json.dumps(x, ensure_ascii=False))
                else:
                    parts.append(str(x))
            return "\n".join(parts)
        return json.dumps(c, ensure_ascii=False)

    def _input_to_messages(psr):
        msgs = []
        instr = psr.get("instructions")
        if isinstance(instr, str) and instr.strip():
            msgs.append({"role": "system", "content": instr})
        inp = psr.get("input")
        if isinstance(inp, str):
            msgs.append({"role": "user", "content": inp})
            return msgs
        if not isinstance(inp, list):
            return msgs
        for it in inp:
            if not isinstance(it, dict):
                msgs.append({"role": "user", "content": str(it)})
                continue
            t = it.get("type") or ("message" if "role" in it else "")
            if t == "message":
                msgs.append({
                    "role": it.get("role", "user"),
                    "content": _content_to_str(it.get("content")),
                })
            elif t == "function_call":
                msgs.append({
                    "role": "assistant",
                    "content": "[tool_call] %s(%s)" % (it.get("name", "?"), it.get("arguments", "")),
                })
            elif t == "custom_tool_call":
                iv = it.get("input", "")
                if not isinstance(iv, str):
                    iv = json.dumps(iv, ensure_ascii=False)
                msgs.append({
                    "role": "assistant",
                    "content": "[custom_tool_call] %s: %s" % (it.get("name", "?"), iv),
                })
            elif t in ("function_call_output", "custom_tool_call_output"):
                o = it.get("output", "")
                if not isinstance(o, str):
                    o = json.dumps(o, ensure_ascii=False)
                msgs.append({"role": "tool", "content": "[tool_result] " + o})
            elif t == "reasoning":
                continue  # encrypted/opaque reasoning blobs — nothing readable
            elif t == "additional_tools":
                tools = it.get("tools") or []
                msgs.append({
                    "role": it.get("role", "developer"),
                    "content": "[additional_tools] %d tool definitions (see Tools panel)" % len(tools),
                })
            else:
                msgs.append({
                    "role": it.get("role", "user"),
                    "content": json.dumps(it, ensure_ascii=False),
                })
        return msgs

    def _transform_payload(res):
        if not isinstance(res, dict):
            return res
        try:
            resp = _maybe_json(res.get("response"))
            if isinstance(resp, dict) and isinstance(resp.get("output"), list):
                changed = False
                new_out = []
                for it in resp["output"]:
                    if isinstance(it, dict) and it.get("type") == "custom_tool_call":
                        args = it.get("input", "")
                        if not isinstance(args, str):
                            args = json.dumps(args, ensure_ascii=False)
                        try:
                            parsed = json.loads(args)
                            if not isinstance(parsed, dict):
                                raise ValueError
                        except Exception:
                            args = json.dumps({"input": args}, ensure_ascii=False)
                        it = dict(it)
                        it["type"] = "function_call"
                        it.setdefault("call_id", it.get("id", ""))
                        it["arguments"] = args
                        changed = True
                    new_out.append(it)
                if changed:
                    resp = dict(resp)
                    resp["output"] = new_out
                    res = dict(res)
                    res["response"] = resp
        except Exception:
            pass
        try:
            psr = _maybe_json(res.get("proxy_server_request"))
            if isinstance(psr, dict) and not psr.get("messages"):
                msgs = _input_to_messages(psr)
                if msgs:
                    psr = dict(psr)
                    psr["messages"] = msgs
                    res = dict(res)
                    res["proxy_server_request"] = psr
        except Exception:
            pass
        return res

    def _wrap(orig):
        if getattr(orig, "_ui_render_fix", False):
            return orig

        @functools.wraps(orig)
        async def wrapped(*args, **kwargs):
            res = await orig(*args, **kwargs)
            try:
                return _transform_payload(res)
            except Exception:
                return res

        wrapped._ui_render_fix = True
        return wrapped

    def _patch_routes(routes, tag):
        n = 0
        for route in routes:
            try:
                if getattr(route, "path", "") != _ROUTE_PATH:
                    continue
                ep = getattr(route, "endpoint", None)
                if ep is not None and not getattr(ep, "_ui_render_fix", False):
                    try:
                        route.endpoint = _wrap(ep)
                    except Exception:
                        pass
                dep = getattr(route, "dependant", None)
                call = getattr(dep, "call", None)
                if call is not None and not getattr(call, "_ui_render_fix", False):
                    dep.call = _wrap(call)
                    n += 1
            except Exception:
                continue
        if n:
            print(
                "[sitecustomize] ui spendlog render fix patched %d route(s) via %s "
                "(responses input -> messages; custom_tool_call -> function_call view)"
                % (n, tag),
                file=sys.stderr,
            )

    def _patch_ep_module(mod):
        try:
            fn = getattr(mod, "ui_view_request_response_for_request_id", None)
            if fn is not None and not getattr(fn, "_ui_render_fix", False):
                mod.ui_view_request_response_for_request_id = _wrap(fn)
            router = getattr(mod, "router", None)
            if router is not None:
                _patch_routes(getattr(router, "routes", []), "spend_management_endpoints.router")
        except Exception as exc:  # pragma: no cover
            print("[sitecustomize] ui render fix ep-module patch failed: " + repr(exc), file=sys.stderr)

    def _patch_ps_module(mod):
        try:
            app = getattr(mod, "app", None)
            if app is not None:
                _patch_routes(getattr(app, "routes", []), "proxy_server.app")
        except Exception as exc:  # pragma: no cover
            print("[sitecustomize] ui render fix app patch failed: " + repr(exc), file=sys.stderr)

    _PATCHERS = {_EP_MOD: _patch_ep_module, _PS_MOD: _patch_ps_module}

    for name, patcher in list(_PATCHERS.items()):
        if name in sys.modules:
            patcher(sys.modules[name])
            del _PATCHERS[name]
    if not _PATCHERS:
        return

    class _UIFixFinder(MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name not in _PATCHERS:
                return None
            try:
                sys.meta_path.remove(self)
            except ValueError:
                pass
            try:
                spec = find_spec(name)
            finally:
                if _PATCHERS and self not in sys.meta_path:
                    sys.meta_path.insert(0, self)
            if spec is None or spec.loader is None:
                return None
            loader = spec.loader
            orig_exec = loader.exec_module
            patcher = _PATCHERS.pop(name, None)

            def exec_module(module):
                orig_exec(module)
                if patcher is not None:
                    patcher(module)

            try:
                loader.exec_module = exec_module
            except Exception:
                pass
            return spec

    sys.meta_path.insert(0, _UIFixFinder())


try:
    _install_ui_spendlog_render_fix()
except Exception as _exc:  # pragma: no cover
    print("[sitecustomize] ui spendlog render fix install failed: " + repr(_exc), file=sys.stderr)
