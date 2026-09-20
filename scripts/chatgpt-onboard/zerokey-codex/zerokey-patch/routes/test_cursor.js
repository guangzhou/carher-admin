const assert = require('assert')
const { CURSOR_GPT_MODELS, resolveCursorModel } = require('./cursor')

const expected = {
  'cursor-gpt-5.6-sol': 'gpt-5.6-sol',
  'cursor-gpt-5.6-terra': 'gpt-5.6-terra',
  'cursor-gpt-5.6-luna': 'gpt-5.6-luna',
  'cursor-gpt-5.5': 'gpt-5-5',
  'cursor-gpt-5.4': 'gpt-5-4-thinking',
  'cursor-gpt-5.3-codex': 'gpt-5-3',
}

assert.deepStrictEqual(CURSOR_GPT_MODELS, expected)
for (const [publicModel, upstreamModel] of Object.entries(expected)) {
  assert.strictEqual(resolveCursorModel(publicModel), upstreamModel)
}
for (const unknown of ['', 'gpt-5.5', 'cursor-gpt-5.6', 'cursor-gpt-4o', null]) {
  assert.strictEqual(resolveCursorModel(unknown), null)
}

console.log(`cursor protocol model mapping: ${Object.keys(expected).length} models OK`)
