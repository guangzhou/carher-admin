// /v1/images/generations — ChatGPT web API image generation bridge.
//
// Sends prompt to /backend-api/f/conversation with model=gpt-image-2 and
// force_use_sse=true. Parses SSE for image_asset_pointer (multimodal_text),
// falls back to polling GET /backend-api/conversation/{cid} if SSE ends
// without delivering the image (async generation).
//
// Returns standard OpenAI /v1/images/generations response format.

const { readSSE } = require('../utils/sse-reader')
const { acquireSlot } = require('../utils/rate-limiter')

const POLL_INITIAL_DELAY = 10000
const POLL_INTERVAL = 6000
const POLL_TIMEOUT = 120000

function buildImagesRoute(chatgptApi) {
  const express = require('express')
  const router = express.Router()

  router.post('/generations', async (req, res) => {
    const {
      prompt,
      model = 'gpt-image-2',
      n = 1,
      response_format = 'b64_json',
    } = req.body

    if (!prompt) {
      return res.status(400).json({
        error: { message: 'prompt is required', type: 'invalid_request_error' },
      })
    }

    // Bump HTTP timeout — image gen takes 45-120s
    req.setTimeout(180000)
    res.setTimeout(180000)

    await acquireSlot('ChatGPT')

    // Web API uses 'auto' — the model auto-routes to DALL-E for image prompts.
    // 'gpt-image-2' as model slug causes null DALL-E prompt in the web conversation.
    let upstream
    try {
      upstream = await chatgptApi.chatCompletion(
        prompt, null, 'client-created-root', 'auto', null, { forceSSE: true },
      )
    } catch (e) {
      return res.status(502).json({
        error: { message: e.message, type: 'upstream_error' },
      })
    }

    let conversationId = null
    const imagePointers = []
    let revisedPrompt = null
    let finished = false

    await readSSE(upstream, {
      onData: (d) => {
        if (finished) return
        _extractFromSSE(d, (cid) => { conversationId = cid }, imagePointers, (p) => { revisedPrompt = p })
      },
      onDone: () => { finished = true },
      onError: (err) => {
        console.error(`[images] SSE error: ${err.message}`)
        if (!finished) finished = true
      },
      isDone: () => finished,
    })

    // SSE delivered the image inline → skip polling
    if (imagePointers.length === 0 && conversationId) {
      console.log(`[images] SSE ended without image, polling ${conversationId}`)
      await _pollForImage(chatgptApi, conversationId, imagePointers, (p) => { revisedPrompt = p })
    }

    if (imagePointers.length === 0) {
      return res.status(504).json({
        error: { message: 'Image generation timed out or failed', type: 'timeout_error' },
      })
    }

    const images = []
    for (const ptr of imagePointers.slice(0, n)) {
      try {
        const img = await chatgptApi.downloadFile(ptr.fileId, conversationId, ptr.protocol)
        if (response_format === 'url') {
          images.push({ url: img.url, revised_prompt: revisedPrompt || prompt })
        } else {
          images.push({ b64_json: img.base64, revised_prompt: revisedPrompt || prompt })
        }
      } catch (e) {
        console.error(`[images] download failed: ${e.message}`)
      }
    }

    if (images.length === 0) {
      return res.status(502).json({
        error: { message: 'Failed to download generated images', type: 'upstream_error' },
      })
    }

    res.json({ created: Math.floor(Date.now() / 1000), data: images })
  })

  return router
}

// ── SSE event extraction ───────────────────────────────────────

function _extractFromSSE(d, setCid, pointers, setPrompt) {
  if (!d) return

  // Top-level conversation_id
  if (d.conversation_id) setCid(d.conversation_id)

  // "add" skeleton — first event has conversation_id
  if (d.o === 'add' && d.v) {
    if (d.v.conversation_id) setCid(d.v.conversation_id)
    _scanForImage(d.v, pointers, setPrompt)
  }

  // "patch" array — progress + completion events
  if (d.o === 'patch' && Array.isArray(d.v)) {
    for (const op of d.v) {
      if (op.v && typeof op.v === 'object') {
        if (op.v.conversation_id) setCid(op.v.conversation_id)
        _scanForImage(op.v, pointers, setPrompt)
      }
    }
  }

  // Direct replace on parts
  if (d.p && d.p.includes('/content/parts/') && d.o === 'replace' && d.v) {
    _scanPart(d.v, pointers, setPrompt)
  }
}

function _scanForImage(obj, pointers, setPrompt) {
  if (!obj) return
  const content = obj.message?.content || obj.content
  if (content?.content_type === 'multimodal_text' && Array.isArray(content.parts)) {
    for (const part of content.parts) _scanPart(part, pointers, setPrompt)
  }
  const dalle = obj.message?.metadata?.dalle || obj.metadata?.dalle
  if (dalle?.prompt) setPrompt(dalle.prompt)
}

function _scanPart(part, pointers, setPrompt) {
  if (!part || typeof part !== 'object') return
  if (part.content_type !== 'image_asset_pointer' || !part.asset_pointer) return
  const ptr = _parsePointer(part.asset_pointer)
  if (ptr && !pointers.some((p) => p.fileId === ptr.fileId)) {
    pointers.push(ptr)
    console.log(`[images] found ${ptr.protocol}://${ptr.fileId}`)
  }
  if (part.metadata?.dalle?.prompt && setPrompt) setPrompt(part.metadata.dalle.prompt)
}

function _parsePointer(s) {
  if (s.startsWith('file-service://')) return { protocol: 'file-service', fileId: s.slice(15) }
  if (s.startsWith('sediment://')) return { protocol: 'sediment', fileId: s.slice(11) }
  return null
}

// ── Conversation polling fallback ──────────────────────────────

async function _pollForImage(api, cid, pointers, setPrompt) {
  await _sleep(POLL_INITIAL_DELAY)
  const deadline = Date.now() + POLL_TIMEOUT
  while (Date.now() < deadline) {
    try {
      const conv = await api.getConversation(cid)
      if (conv?.mapping) {
        for (const node of Object.values(conv.mapping)) {
          const msg = node.message
          if (!msg) continue
          if (msg.content?.content_type === 'multimodal_text') {
            for (const part of msg.content.parts || []) _scanPart(part, pointers, setPrompt)
            if (msg.metadata?.dalle?.prompt) setPrompt(msg.metadata.dalle.prompt)
          }
        }
        if (pointers.length > 0) {
          console.log(`[images] poll found ${pointers.length} image(s)`)
          return
        }
      }
    } catch (e) {
      console.log(`[images] poll error: ${e.message}`)
    }
    await _sleep(POLL_INTERVAL)
  }
  console.log('[images] poll timed out')
}

function _sleep(ms) { return new Promise((r) => setTimeout(r, ms)) }

module.exports = { buildImagesRoute }
