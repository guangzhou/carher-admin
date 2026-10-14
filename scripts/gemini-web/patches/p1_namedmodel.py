"""Gemini-FastAPI 1731445 × gemini_webapi 2.1.1: custom models crash on Model.from_dict.
Pass the model name through as a str so the library resolves it per account."""
p = "/root/gemini-web-build/src/app/server/chat.py"
s = open(p).read()
old = """    if name in custom_models:
        return Model.from_dict(custom_models[name].model_dump())
"""
new = """    if name in custom_models:
        # carher patch: gemini_webapi 2.1 removed Model.from_dict; the library resolves a str
        # against each account's live model registry (aliases include gemini-4-argon).
        return _NamedModel(name)
"""
assert old in s, "anchor missing"
s = s.replace(old, new)
anchor = "def _get_model_by_name(name: str) -> Model:"
s = s.replace(anchor, '''class _NamedModel(str):
    """str that also exposes .model_name, which the LMDB session code reads."""

    @property
    def model_name(self) -> str:
        return str(self)


''' + anchor)
open(p, "w").write(s)
print("patched")
