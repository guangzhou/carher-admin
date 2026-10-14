"""carher p3.1: _carher_chat_inner returns a ChatCompletionResponse (pydantic), not a dict, so p3's
isinstance(payload, dict) checks never fired: no retry, and stream requests got a plain JSON body.
Normalize to dict right after each inner call."""
SRC = "/root/gemini-web-build/src/app/server/chat.py"
s = open(SRC).read()
assert "carher p3.1" not in s, "already patched"
helper = '''

def _carher_as_dict(resp):  # carher p3.1
    if isinstance(resp, dict):
        return resp
    dump = getattr(resp, "model_dump", None)
    return dump(mode="json") if callable(dump) else resp
'''
anchor = "\n\ndef _carher_wants_tools("
assert s.count(anchor) == 1
s = s.replace(anchor, helper + anchor, 1)
old1 = "    payload = await _carher_chat_inner(first, raw_request, api_key, tmp_dir, image_store)\n"
assert s.count(old1) == 1
s = s.replace(old1, "    payload = _carher_as_dict(await _carher_chat_inner(first, raw_request, api_key, tmp_dir, image_store))\n", 1)
old2 = "            second = await _carher_chat_inner(retry, raw_request, api_key, tmp_dir, image_store)\n"
assert s.count(old2) == 1
s = s.replace(old2, "            second = _carher_as_dict(await _carher_chat_inner(retry, raw_request, api_key, tmp_dir, image_store))\n", 1)
open(SRC, "w").write(s)
print("patched p3.1")
