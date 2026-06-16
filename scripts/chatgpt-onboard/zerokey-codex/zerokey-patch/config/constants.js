/**
 * Application constants and configuration.
 */

const CONFIG = {
  PORT: process.env.PORT || 8000,
}

// Real chatgpt.com web models available to the captured Pro account.
// Source: GET https://chatgpt.com/backend-api/models
const WEB_MODEL_SLUGS = [
  'gpt-5-5-pro',
  'gpt-5-5-thinking',
  'gpt-5-5',
  'gpt-5-5-instant',
  'gpt-5-4-pro',
  'gpt-5-4-thinking',
  'gpt-5-4-t-mini',
  'gpt-5-3',
  'gpt-5-3-instant',
  'gpt-5-3-mini',
  'gpt-5-2',
  'gpt-5-1',
  'gpt-5',
  'gpt-5-mini',
  'o3',
  'o3-pro',
  'gpt-4-5',
  'research',
  'agent-mode',
]

const MODELS = WEB_MODEL_SLUGS.reduce((acc, slug) => {
  acc[slug] = {
    id: slug,
    object: 'model',
    created: 1_784_736_000,
    owned_by: 'openai-chatgpt-web',
  }
  return acc
}, {})

module.exports = { CONFIG, MODELS }
