"""
Patch ResponsesAPIResponse.model_construct to ensure .output always exists.

Root cause (verified 2026-05-27):
  LiteLLM v1.83.14 / v1.85.0 in `llms/openai/responses/transformation.py` lines
  235-242 and 635-647 uses a try/except where strict construction
  `ResponsesAPIResponse(**raw_response_json)` raises ValidationError when the
  upstream ChatGPT pool returns a body missing the `output` field (a documented
  edge case for short prompts / refusals where the model produces only
  reasoning_tokens and no message item). The except branch then uses
  `ResponsesAPIResponse.model_construct(**raw_response_json)` which silently
  creates a half-constructed Pydantic object lacking the `output` attribute.
  Downstream code that iterates `response.output` (emulated_handler.py:328,
  mcp_handler.py:521, utils.py multiple, redact_messages.py:178, etc.) then
  crashes with `AttributeError: 'ResponsesAPIResponse' object has no attribute
  'output'`, which surfaces as HTTP 400
  `Invalid request format: ... 'ResponsesAPIResponse' object has no attribute 'output'`.

Fix: monkey-patch `ResponsesAPIResponse.model_construct` to inject
`output=[]` when missing/None, before the underlying construct runs. This
turns the previously-crashing "for item in response.output" loops into
zero-iteration no-ops, allowing transform / dump to complete cleanly.

Risk profile (validated in Phase 0 REPL):
  * Strict construct path (ResponsesAPIResponse(**raw)) unchanged - still
    raises ValidationError on missing required fields.
  * Existing valid responses (output present, non-None) untouched.
  * `model_dump()` output gains `"output": []` when previously omitted - this
    is more schema-conformant than missing field; OpenAI Python SDK and any
    other compliant client tolerate empty list fine.

Activation: this module is imported via the litellm callbacks list. Module-level
import side-effect installs the patch before LiteLLM begins handling requests.
"""
from litellm.integrations.custom_logger import CustomLogger
from litellm.types.llms.openai import ResponsesAPIResponse

_orig_model_construct = ResponsesAPIResponse.model_construct.__func__


def _patched_model_construct(cls, _fields_set=None, **values):
    if "output" not in values or values.get("output") is None:
        values["output"] = []
    return _orig_model_construct(cls, _fields_set=_fields_set, **values)


ResponsesAPIResponse.model_construct = classmethod(_patched_model_construct)

print(
    "[chatgpt_responses_output_fallback] patched "
    "ResponsesAPIResponse.model_construct: .output always exists when "
    "constructed via the model_construct fallback path"
)


class ChatGPTResponsesOutputFallback(CustomLogger):
    """No-op callback. The actual fix is the module-import side effect
    (monkey-patch above). The callback is only registered so LiteLLM
    imports this module on startup."""

    pass


chatgpt_responses_output_fallback = ChatGPTResponsesOutputFallback()
