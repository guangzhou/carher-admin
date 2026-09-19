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

# ─────────────────────────────────────────────────────────────────────────────
# SPEND-LOG BATCH RESCUE  (root-caused 2026-09-19, LiteLLM 1.100.1 prod on 198)
# ─────────────────────────────────────────────────────────────────────────────
# SYMPTOM  Two ERROR lines appear together, ~4x/min per gray pod:
#   utils.py:6238            "spend log queue is at its 64000000 byte budget;
#                             dropped the N oldest spend logs"
#   spend_log_error_logger.py:81
#                            "Error in spend logs queue monitor: Unable to match
#                             input value to any allowed input type for the
#                             field. ... `data` should be of any of the
#                             following types: `LiteLLM_SpendLogsCreateManyInput`"
#
# ROOT CAUSE  ``update_spend_logs_job`` (utils.py:6522) POPS the batch off the
# queue (``dequeue_spend_logs``, 6542) BEFORE writing it. Inside
# ``ProxyUpdateSpend.update_spend_logs`` (6320) only two paths put rows back:
#   * asyncio.CancelledError            -> enqueue_spend_logs(at_head=True)
#   * DB *transport* error, retries out -> enqueue_spend_logs(at_head=True)
# and 6384-6386 reads:
#     if not PrismaDBExceptionHandler.is_database_transport_error(e): raise
#
# CORRECTION (measured later the same day; two earlier readings of this were
# wrong and are retracted here). The prisma engine's input-validation error DOES
# get classified as a transport error, because
# ``is_database_transport_error`` matches KEYWORDS against ``str(e)`` and a
# DataError's message EMBEDS THE REJECTED ROW, whose payload routinely contains
# "timeout" / "connection error". Measured: 16 of 16 DataError lines matched.
# So the real shape is NOT "the batch is silently lost" but:
#     bad batch -> judged transport -> retried 4x ("retry 4/3" seen 26 times)
#     -> enqueue_spend_logs(at_head=True) -> SAME batch popped next flush
#     -> forever, sitting in FRONT of the rows behind it.
# That misclassification is fixed by the second installer at the bottom of this
# file; this first installer is what then bounds the damage to the few rows that
# genuinely cannot be written.
#
# ``_create_spend_logs_with_poison_isolation`` (6731) cannot help: it bisects to
# find the row that makes the *DB* reject a statement, but here the statement is
# refused by the engine's argument parser before it is ever sent, so every
# bisection half fails identically and the isolation budget just burns.
#
# EVIDENCE (2026-09-19, gray pods, v1.100.1)
#   * 13 distinct ``litellm_call_id`` values scraped off the rejected-batch log
#     lines were queried against LiteLLM_SpendLogs.request_id: ALL 13 absent.
#     Negative control on the same SQL with 3 known-present request_ids
#     (including a 36-char UUID-shaped one) returned 1 each => the 0s are real
#     data loss, not a bad query.
#   * Landing curve has NO gap (19 consecutive 5-min buckets, 58..222 rows,
#     newest-row lag 18s) and gpt-5.6-sol still lands 204 rows/30min => the loss
#     is a small per-batch fraction, not an outage.
#   * v1.90.2 (guarded-old) never emits this line: 1.100.1-only.
#
# FIX  Wrap ``ProxyUpdateSpend.update_spend_logs``. On ANY exception that is not
# already handled by upstream's requeue paths, put the batch BACK AT THE HEAD of
# the queue, but stamp each row with a rescue counter first. A row that has been
# rescued more than ``_MAX_RESCUES`` times is dropped ALONE and loudly, so one
# permanently-unwritable row can never hold the queue hostage (which would
# convert a small loss into a total stall — strictly worse).
#
# Invariants this must preserve:
#   1. NEVER swallow the exception. Upstream's caller
#      (``_raise_failed_update_spend_exception``) and the alerting that hangs off
#      it must still see the failure. We re-raise unconditionally.
#   2. NEVER requeue on CancelledError or on transport errors — upstream already
#      did it there; doing it twice would duplicate rows.
#   3. The rescue counter lives OUTSIDE the row dict sent to the DB (a private
#      side table keyed by id()) — adding a key to the row would itself be an
#      "unknown field" and cause the very error we are fixing.
#   4. Fail-open: any error inside this wrapper is swallowed after the re-raise
#      of the original, so the hook can never make the flush worse.
#
# Log level is WARNING/ERROR on purpose: ``log.info`` from a sitecustomize
# patch is invisible (it runs before the app installs handlers, so logging's
# lastResort handler at level WARNING is what prints).
# ─────────────────────────────────────────────────────────────────────────────


def _install_spendlog_batch_rescue():
    import asyncio
    from importlib.abc import MetaPathFinder
    from importlib.util import find_spec

    _TARGET = "litellm.proxy.utils"
    _MAX_RESCUES = 3

    # request_id -> times this row came back from a failed write.
    # Keyed by request_id (a str already on every row) so it survives the
    # dict being rebuilt by jsonify_object. Bounded to avoid unbounded growth.
    _rescues = {}
    _RESCUES_MAX_KEYS = 50000

    def _row_key(row):
        try:
            rid = row.get("request_id")
            return rid if isinstance(rid, str) else None
        except Exception:
            return None

    def _split_batch(logs):
        """Return (retry_rows, doomed_rows) by rescue count."""
        retry, doomed = [], []
        for row in logs:
            k = _row_key(row)
            if k is None:
                # No id to track: retry once-ish but never forever. Treat as
                # doomed so it cannot become an untracked permanent blocker.
                doomed.append(row)
                continue
            n = _rescues.get(k, 0) + 1
            if len(_rescues) < _RESCUES_MAX_KEYS:
                _rescues[k] = n
            if n > _MAX_RESCUES:
                _rescues.pop(k, None)
                doomed.append(row)
            else:
                retry.append(row)
        return retry, doomed

    def _clear_rescues(logs):
        for row in logs:
            k = _row_key(row)
            if k is not None:
                _rescues.pop(k, None)

    def _patch(mod):
        try:
            P = getattr(mod, "ProxyUpdateSpend", None)
            enqueue = getattr(mod, "enqueue_spend_logs", None)
            handler = getattr(mod, "PrismaDBExceptionHandler", None)
            if P is None or enqueue is None:
                print(
                    "[sitecustomize] spendlog batch rescue: hook point missing "
                    "(ProxyUpdateSpend=%r enqueue_spend_logs=%r) - NOT installed"
                    % (P is not None, enqueue is not None),
                    file=sys.stderr,
                )
                return
            orig = P.update_spend_logs
            if getattr(orig, "_spendlog_batch_rescue_patched", False):
                return

            import functools

            @functools.wraps(orig)
            async def patched(*args, **kwargs):
                logs = kwargs.get("logs_to_process")
                if logs is None:
                    # Caller did not pass the batch; upstream pops it itself and
                    # we have no handle on it. Nothing to rescue - pass through.
                    return await orig(*args, **kwargs)
                try:
                    result = await orig(*args, **kwargs)
                except asyncio.CancelledError:
                    raise                      # invariant 2: upstream requeued
                except Exception as exc:
                    try:
                        transport = False
                        if handler is not None:
                            try:
                                transport = bool(
                                    handler.is_database_transport_error(exc)
                                )
                            except Exception:
                                transport = False
                        if not transport and logs:
                            # Callers invoke update_spend_logs with keyword
                            # args only; never guess from *args -- passing the
                            # wrong object to enqueue would corrupt the queue.
                            prisma_client = kwargs.get("prisma_client")
                            retry, doomed = _split_batch(logs)
                            if doomed:
                                print(
                                    "[spendlog-rescue] ERROR dropping %d spend "
                                    "log row(s) the DB engine refused %d times "
                                    "(request_ids: %s); error=%s"
                                    % (
                                        len(doomed),
                                        _MAX_RESCUES,
                                        ",".join(
                                            str(_row_key(r)) for r in doomed[:10]
                                        ),
                                        str(exc)[:300].replace("\n", " "),
                                    ),
                                    file=sys.stderr,
                                )
                            if retry and prisma_client is not None:
                                await enqueue(
                                    prisma_client, retry, at_head=True
                                )
                                print(
                                    "[spendlog-rescue] WARNING requeued %d of "
                                    "%d spend log rows after a non-transport "
                                    "write failure (upstream would have lost "
                                    "the whole batch)"
                                    % (len(retry), len(logs)),
                                    file=sys.stderr,
                                )
                    except Exception as inner:   # invariant 4: fail-open
                        print(
                            "[spendlog-rescue] ERROR rescue path itself failed: "
                            + repr(inner),
                            file=sys.stderr,
                        )
                    raise                        # invariant 1: never swallow
                else:
                    _clear_rescues(logs)
                    return result

            patched._spendlog_batch_rescue_patched = True
            P.update_spend_logs = staticmethod(patched)
            print(
                "[sitecustomize] spendlog batch rescue patched "
                "ProxyUpdateSpend.update_spend_logs (requeues a batch the "
                "prisma engine refuses instead of losing it; drops a row alone "
                "after %d rescues)" % _MAX_RESCUES,
                file=sys.stderr,
            )
        except Exception as exc:  # pragma: no cover
            print(
                "[sitecustomize] spendlog batch rescue patch failed: " + repr(exc),
                file=sys.stderr,
            )

    if _TARGET in sys.modules:
        _patch(sys.modules[_TARGET])
        return

    class _RescueFinder(MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != _TARGET:
                return None
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
                orig_exec(module)
                _patch(module)

            try:
                loader.exec_module = exec_module
            except Exception:
                pass
            return spec

    sys.meta_path.insert(0, _RescueFinder())


try:
    _install_spendlog_batch_rescue()
except Exception as _exc:  # pragma: no cover
    print(
        "[sitecustomize] spendlog batch rescue install failed: " + repr(_exc),
        file=sys.stderr,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Patch #2 for spend-log writes: stop a data error from being mistaken for a
# transport error.
#
# SYMPTOM (measured on litellm-proxy-gray, 2026-09-19 13:12-13:13 UTC):
#   prisma.errors.DataError "Unable to match input value to any allowed input
#   type for the field ... `LiteLLM_SpendLogsCreateManyInput` ... is not a
#   valid `JSON String`. Underlying error: invalid escape"
#   accompanied, in the SAME second, by
#   "Spend tracking - DB connection error writing spend logs, retry 1/3 .. 4/3".
#
# ROOT CAUSE (litellm/proxy/db/exception_handler.py:127
# PrismaDBExceptionHandler.is_database_transport_error):
#   For any prisma.errors.PrismaError it lowercases str(e) and returns True if
#   the message contains any of "timeout", "timed out", "connection error", ...
#   A DataError's message EMBEDS THE REJECTED ROW, and spend-log rows routinely
#   contain those words in their request/response payloads. Measured: 16 of 16
#   DataError lines contained "timeout"/"timed out"/"connection error".
#   So a pure input-validation error is classified as a connectivity failure.
#
# CONSEQUENCE (litellm/proxy/utils.py:6384-6400, update_spend_logs):
#   transport => retry loop runs, and at i >= n_retry_times it calls
#   enqueue_spend_logs(..., at_head=True). The batch goes back to the HEAD of
#   the queue and the next flush pops the same poisoned batch again. Observed
#   "retry 4/3" 26 times. The bad batch never drains and, being at the head,
#   it sits in front of the rows behind it. Reconciliation: 13 request_ids
#   scraped from rejected batches were all absent from LiteLLM_SpendLogs, while
#   3 known-good request_ids each returned 1 (negative control valid).
#
# WHY THIS AND NOT A CONTENT SCRUBBER:
#   The offending character is not recoverable from either side: litellm's own
#   DB-storage truncation ("litellm_truncated skipped N chars") removes the
#   region from the logged message, and the row never lands so it cannot be
#   read back. Negative control ruled truncation out as the cause: 1583 of 1678
#   rows that DID land in the last hour carry the same truncation marker. The
#   classification bug, by contrast, is proven by data and is what turns one
#   bad row into an unbounded head-of-queue retry.
#
# WHAT THIS DOES:
#   Wrap is_database_transport_error so that a prisma DataError / a message
#   whose connectivity verdict comes only from text found inside the embedded
#   row is reported as NOT transport. update_spend_logs then takes the bare
#   `raise` at 6385, the batch is not re-enqueued at the head, and patch #1
#   (the batch rescue installed above) takes over: it requeues the batch up to
#   _MAX_RESCUES times and then drops only the rows that can never be written,
#   logging their request_ids.
#
# Invariants:
#   1. Only ever flips True -> False, never False -> True. A real connectivity
#      failure must keep its retry/reconnect behaviour.
#   2. Only for prisma data errors. Any other exception type is passed straight
#      through to the original classifier.
#   3. Fail-open: if anything in here raises, defer to the original verdict.
#   4. No content is logged. The rejected row embeds real users' key metadata
#      (emails, open_ids, team spend), so only the exception class name and a
#      row count are printed.
# ─────────────────────────────────────────────────────────────────────────────


def _install_spendlog_transport_misclassification_fix():
    from importlib.abc import MetaPathFinder
    from importlib.util import find_spec

    _TARGET = "litellm.proxy.db.exception_handler"

    def _is_data_error(exc):
        """True iff exc is a prisma error that means 'the DB refused this input'.

        Such an error proves the DB was REACHED, so reconnect/retry logic must
        not claim it. Matched by class name rather than by isinstance so a
        prisma version that moves the class does not silently disable the fix.
        """
        try:
            import prisma.errors as pe
        except Exception:
            return False
        data_error_types = tuple(
            t
            for t in (
                getattr(pe, "DataError", None),
                getattr(pe, "MissingRequiredValueError", None),
                getattr(pe, "UniqueViolationError", None),
                getattr(pe, "ForeignKeyViolationError", None),
                getattr(pe, "RecordNotFoundError", None),
            )
            if isinstance(t, type)
        )
        if data_error_types and isinstance(exc, data_error_types):
            return True
        return type(exc).__name__ in (
            "DataError",
            "MissingRequiredValueError",
            "UniqueViolationError",
            "ForeignKeyViolationError",
        )

    def _patch(mod):
        try:
            handler = getattr(mod, "PrismaDBExceptionHandler", None)
            if handler is None:
                print(
                    "[sitecustomize] transport-misclassification fix: "
                    "PrismaDBExceptionHandler missing - NOT installed",
                    file=sys.stderr,
                )
                return
            orig = handler.is_database_transport_error
            if getattr(orig, "_transport_misclassification_fixed", False):
                return

            import functools

            _reported = [0]

            @functools.wraps(orig)
            def patched(e):
                try:
                    verdict = orig(e)
                except Exception:
                    raise
                try:
                    # invariant 1: only ever narrow a True verdict.
                    if verdict and _is_data_error(e):
                        if _reported[0] < 20:
                            _reported[0] += 1
                            print(
                                "[spendlog-transport-fix] WARNING reclassified "
                                "%s as NON-transport (the DB was reached and "
                                "refused the input; its message merely embeds "
                                "the row, which contains connectivity words). "
                                "Upstream will no longer requeue it at the "
                                "queue head."
                                % type(e).__name__,
                                file=sys.stderr,
                            )
                        return False
                except Exception as inner:  # invariant 3: fail-open
                    print(
                        "[spendlog-transport-fix] ERROR fix path failed, "
                        "deferring to original verdict: " + repr(inner),
                        file=sys.stderr,
                    )
                return verdict

            patched._transport_misclassification_fixed = True
            handler.is_database_transport_error = staticmethod(patched)
            print(
                "[sitecustomize] transport-misclassification fix patched "
                "PrismaDBExceptionHandler.is_database_transport_error "
                "(prisma data errors no longer counted as connectivity "
                "failures)",
                file=sys.stderr,
            )
        except Exception as exc:  # pragma: no cover
            print(
                "[sitecustomize] transport-misclassification fix failed: "
                + repr(exc),
                file=sys.stderr,
            )

    if _TARGET in sys.modules:
        _patch(sys.modules[_TARGET])
        return

    class _TransportFixFinder(MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != _TARGET:
                return None
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
                orig_exec(module)
                _patch(module)

            try:
                loader.exec_module = exec_module
            except Exception:
                pass
            return spec

    sys.meta_path.insert(0, _TransportFixFinder())


try:
    _install_spendlog_transport_misclassification_fix()
except Exception as _exc:  # pragma: no cover
    print(
        "[sitecustomize] transport-misclassification install failed: "
        + repr(_exc),
        file=sys.stderr,
    )
