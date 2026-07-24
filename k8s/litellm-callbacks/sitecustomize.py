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
# Diagnosis proof: utils.py:5591 still calls ``prisma_client.jsonify_object``
# (so the hook point is correct on 1.90.2), and manual ``import
# null_byte_sanitize`` in-pod scrubs ``a<NUL>b`` -> ``ab`` correctly — the
# only gap was that the running flush worker had not applied the patch.
#
# Remove once upstream LiteLLM ships its own sanitization (BerriAI/litellm
# #21290 / #24310 / #19847) or the callback import is made eager per-worker.

import sys

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

            # Content fields dropped from spend-log rows. These carry the
            # prompt/response/raw-body text that is the source of the
            # PG/prisma-poison bytes (lone surrogates, NUL, oversized truncated
            # JSON) that reject whole create_many batches — dropping them
            # removes the poison at the root so clean rows always land. Chosen
            # tradeoff (2026-07-23): keep per-request metadata/tokens/cost/model
            # /key, drop message content. Equivalent to store_prompts_in_spend
            # _logs=false + log_raw_request_response=false but enforced at the
            # last step before create_many, independent of config/env.
            _CONTENT_KEYS = ("messages", "response", "proxy_server_request")

            def _is_spendlog_row(d):
                # jsonify_object is generic (used for many tables); only touch
                # rows that look like a SpendLogs payload.
                return "request_id" in d and ("spend" in d or "startTime" in d)

            def patched_jsonify_object(self, data):
                try:
                    if isinstance(data, dict):
                        if _is_spendlog_row(data):
                            for _k in _CONTENT_KEYS:
                                if _k in data:
                                    data[_k] = {}
                        data = _scrub(data)
                except Exception:
                    pass  # fail-open: never block a spend-log write on the hook
                return orig(self, data)

            patched_jsonify_object._spendlog_sanitize_patched = True
            PrismaClient.jsonify_object = patched_jsonify_object
            print(
                "[sitecustomize] spendlog sanitizer patched "
                "PrismaClient.jsonify_object (drops messages/response/"
                "proxy_server_request + scrubs \\x00 / \\u0000 / lone "
                "surrogates before create_many)",
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
