/**
 * Application constants and configuration.
 */

const CONFIG = {
  PORT: process.env.PORT || 8000,
}

// Real chatgpt.com web models available to the captured Pro account.
// Source: GET https://chatgpt.com/backend-api/models  (re-measured 2026-07-27)
//
// IMPORTANT -- what this list does and does NOT do:
//   It ONLY feeds GET /v1/models (advertising). It does NOT gate requests.
//   Measured: core/chatgpt/api.js passes `model` VERBATIM upstream
//   (`model: model || 'auto'`, no table lookup), so an unlisted slug still
//   works and even a bogus slug returns 200. Verified in-pod:
//     gpt-5.6-sol-wm -> 200,  gpt-5-6-pro -> 200,  bogus-model-xyz -> 200
//   So this is a CATALOGUE, not an allowlist. Keeping it accurate still matters:
//   clients (LiteLLM model discovery, IDE model pickers) read it to decide what
//   to offer, and a stale catalogue hides real models while offering dead ones.
//
// Retired slugs removed in this pass -- upstream still 200s on them but they no
// longer appear in /backend-api/models, so advertising them is a lie:
//   gpt-5-4-pro, gpt-5-4-thinking, gpt-5-2, gpt-5-1, gpt-5, gpt-5-mini,
//   gpt-4-5, agent-mode
//
// NOTE on the two slug spellings: /backend-api/models returns BOTH dotted `-wm`
// slugs (gpt-5.6-sol-wm) and hyphenated ones (gpt-5-5-pro). They are the same
// family in different notations; both are listed because both are real entries
// in the upstream catalogue.
const WEB_MODEL_SLUGS = [
  // 410k context -- largest available
  'gpt-5-6-pro',
  'gpt-5-5-pro',
  'gpt-5-5-thinking',
  // 262k context
  'gpt-5-6-thinking',
  'gpt-5.6-sol-wm',
  'gpt-5.6-terra-wm',
  'gpt-5.6-luna-wm',
  'gpt-5.5-wm',
  'gpt-5.5-cca-wm',
  'gpt-5-4-t-mini',
  // 196k context
  'o3',
  'o3-pro',
  // 137k context
  'gpt-5-5',
  'gpt-5-5-instant',
  'gpt-5-5-mini',
  'gpt-5-3',
  'gpt-5-3-instant',
  // 128k context
  'gpt-5-3-mini',
  // Deep Research (34k)
  'research',
]

// Max context per slug, from the same /backend-api/models response. Surfaced on
// /v1/models so a client can pick a model by context size instead of guessing.
const WEB_MODEL_CONTEXT = {
  'gpt-5-6-pro': 410000,
  'gpt-5-5-pro': 410000,
  'gpt-5-5-thinking': 410000,
  'gpt-5-6-thinking': 262144,
  'gpt-5.6-sol-wm': 262144,
  'gpt-5.6-terra-wm': 262144,
  'gpt-5.6-luna-wm': 262144,
  'gpt-5.5-wm': 262144,
  'gpt-5.5-cca-wm': 262144,
  'gpt-5-4-t-mini': 262144,
  o3: 196608,
  'o3-pro': 196608,
  'gpt-5-5': 137000,
  'gpt-5-5-instant': 137000,
  'gpt-5-5-mini': 137000,
  'gpt-5-3': 137000,
  'gpt-5-3-instant': 137000,
  'gpt-5-3-mini': 128000,
  research: 34815,
}

// Tool recipients this pod can actually HARVEST from a web-injection stream.
//
// The model self-reports ~34 namespaces (gmail.*, gcal.*, container.download,
// container.feed_chars, container.open_image, bio.update, ...), but aggregating
// every SSE capture from the 2026-07-27 measurements showed only five recipients
// ever fire, and only these two carry a harvestable `content_type:"code"` body:
//   container.exec  (shell)      python  (code interpreter)
// The other three that fire are api_tool.call_tool / api_tool.list_resources
// (MCP connector plane -- handled server-side by OpenAI, nothing to harvest) and
// web.run (no local execution).
//
// container.download/feed_chars/open_image NEVER fired, including when the model
// was asked outright for a download link -- it replied with a plain
// `sandbox:/mnt/data/x.csv` text link instead. Adding harvest support for them
// would be dead code; recorded here so nobody re-derives it.
// See docs/zerokey-bridge/web-capability-inventory.md
const HARVESTABLE_RECIPIENTS = ['container.exec', 'python']

const MODELS = WEB_MODEL_SLUGS.reduce((acc, slug) => {
  acc[slug] = {
    id: slug,
    object: 'model',
    created: 1_784_736_000,
    owned_by: 'openai-chatgpt-web',
    // Non-standard but harmless extra field; clients that don't know it ignore it.
    context_window: WEB_MODEL_CONTEXT[slug] || null,
  }
  return acc
}, {})

module.exports = {
  CONFIG,
  MODELS,
  WEB_MODEL_SLUGS,
  WEB_MODEL_CONTEXT,
  HARVESTABLE_RECIPIENTS,
}
