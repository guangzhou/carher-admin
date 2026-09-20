// Cursor-specific Chat Completions protocol for zerokey.
//
// LiteLLM's cursor_responses_shim converts Cursor's Responses-shaped
// /chat/completions body before it reaches this handler. This module only owns
// the Cursor model namespace and delegates the already-valid OpenAI request to
// the existing raw path. The `vscode` ToolCompiler and `raw` protocol branches
// therefore remain unchanged.

// Public Cursor model name -> verified ChatGPT web model slug. Keep this list
// explicit: a new Cursor model needs an intentional adapter and key grant.
const CURSOR_GPT_MODELS = Object.freeze({
  'cursor-gpt-5.6-sol': 'gpt-5.6-sol',
  'cursor-gpt-5.6-terra': 'gpt-5.6-terra',
  'cursor-gpt-5.6-luna': 'gpt-5.6-luna',
  'cursor-gpt-5.5': 'gpt-5-5',
  'cursor-gpt-5.4': 'gpt-5-4-thinking',
  'cursor-gpt-5.3-codex': 'gpt-5-3',
})

function resolveCursorModel(model) {
  return typeof model === 'string' ? CURSOR_GPT_MODELS[model] || null : null
}

function unsupportedModel(res, model) {
  return res.status(400).json({
    error: {
      message: `Unsupported Cursor zerokey model: ${model || '(missing model)'}`,
      type: 'invalid_request_error',
      code: 'model_not_found',
    },
  })
}

async function cursorComplete(req, res, chatgptApi, rawComplete) {
  const requested = req.body && req.body.model
  const upstream = resolveCursorModel(requested)
  if (!upstream) return unsupportedModel(res, requested)

  // rawComplete emits the client's public model in responses. Pass an isolated
  // request object so its model resolver receives the intended web slug.
  const cursorReq = Object.create(req)
  cursorReq.body = { ...req.body, model: upstream }
  return rawComplete(cursorReq, res, chatgptApi, { responseModel: requested })
}

module.exports = { CURSOR_GPT_MODELS, cursorComplete, resolveCursorModel }
