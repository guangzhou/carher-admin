"""carher p3.2: widen the prose-instead-of-tool pattern (English 'you can share/paste', 'To view ... you',
'I don't have access') and allow up to 2 nudged retries instead of 1."""
SRC = "/root/gemini-web-build/src/app/server/chat.py"
s = open(SRC).read()
assert "carher p3.2" not in s, "already patched"
old = '''    r"you can (run|use|execute)|please (paste|share|provide)|"'''
new = '''    r"you can (run|use|execute|share|paste|provide|copy|open)|please (paste|share|provide|run)|"  # carher p3.2
    r"\\bto (view|see|check|inspect|look at|analy[sz]e)\\b[^.\\n]{0,80}\\byou\\b|"
    r"(i|I) (do not|don't|can't|cannot) have (direct )?access|"'''
assert s.count(old) == 1, "regex anchor"
s = s.replace(old, new, 1)
old_retry = '''    if isinstance(payload, dict) and _carher_needs_retry(payload):
        logger.warning("[carher p3] tool request answered in prose; retrying once with nudge")
        retry = _carher_nudged(first)
        try:
            second = _carher_as_dict(await _carher_chat_inner(retry, raw_request, api_key, tmp_dir, image_store))
            if isinstance(second, dict):
                payload = second
                logger.info(f"[carher p3] retry tool_calls={bool(second['choices'][0]['message'].get('tool_calls'))}")
        except HTTPException as exc:
            logger.warning(f"[carher p3] retry failed ({exc.status_code}); keeping first answer")
'''
new_retry = '''    for attempt in (1, 2):
        if not (isinstance(payload, dict) and _carher_needs_retry(payload)):
            break
        logger.warning(f"[carher p3] tool request answered in prose; nudged retry {attempt}/2")
        retry = _carher_nudged(first)
        try:
            second = _carher_as_dict(await _carher_chat_inner(retry, raw_request, api_key, tmp_dir, image_store))
            if isinstance(second, dict):
                payload = second
                logger.info(f"[carher p3] retry{attempt} tool_calls={bool(second['choices'][0]['message'].get('tool_calls'))}")
        except HTTPException as exc:
            logger.warning(f"[carher p3] retry{attempt} failed ({exc.status_code}); keeping previous answer")
            break
'''
assert s.count(old_retry) == 1, "retry anchor"
s = s.replace(old_retry, new_retry, 1)
open(SRC, "w").write(s)
print("patched p3.2")
