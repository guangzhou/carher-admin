const crypto = require('crypto')
const { ChatGPTProofOfWork } = require('./pow')
const { CookieJar } = require('../../utils/cookie-jar')

/**
 * ChatGPT API Client
 *
 * User pastes a full fetch() call from browser DevTools.
 * We extract headers + body, reuse all real values.
 *
 * CRITICAL: "Copy as fetch" omits User-Agent (browser adds it automatically).
 * We extract the real UA from the proof token config[4] and add it to every request.
 * Without User-Agent, Cloudflare returns 403.
 */
class ChatGPTAPI {
  constructor() {
    this.BASE_URL = 'https://chatgpt.com'
    this._headers = null
    this._bodyTemplate = null
    this._config = null
    this._ready = false
    this._cookies = new CookieJar()
  }

  async initializeFromJSON(data) {
    this._headers = data.headers
    this._bodyTemplate = data.body

    // Seed cookie jar from initial headers
    const initialCookie = this._headers.cookie || this._headers.Cookie || ''
    if (initialCookie) {
      const count = this._cookies.seedFromHeader(initialCookie)
      console.log(`[ChatGPT] Seeded cookie jar with ${count} initial cookies`)
    }

    const existingProof = this._headers['openai-sentinel-proof-token']
    if (!existingProof) {
      throw new Error('openai-sentinel-proof-token not found in headers. Data is incomplete.')
    }
    this._config = ChatGPTProofOfWork.decodeProofToken(existingProof)

    const realUA = this._config[4]
    if (realUA && typeof realUA === 'string' && realUA.length > 20) {
      this._headers['user-agent'] = realUA
      console.log('[ChatGPT] Extracted user-agent from proof token:', realUA.slice(0, 60) + '...')
    }

    console.log('[ChatGPT] Loaded from saved JSON')

    await this._refreshSentinel()
    this._ready = true
  }

  async chatCompletion(prompt, chatSessionId, parentMessageId = 'client-created-root', model = null, attachments = null) {
    if (!this._ready) throw new Error('Not initialized')

    await this._refreshSentinel()

    // Always prepare conversation before sending — matches browser HAR flow.
    await this._prepareConversation(chatSessionId, parentMessageId, model, attachments)

    const messageId = crypto.randomUUID()
    const now = Date.now() / 1000

    // Build body from scratch (matching gptchat2api-cf startTextConversation).
    // Cloning the captured _bodyTemplate caused file-attachment conversations to
    // fail — stale fields in the template prevent the model from reading files.
    const metadata = {
      developer_mode_connector_ids: [],
      selected_github_repos: [],
      selected_all_github_repos: false,
      serialization_metadata: { custom_symbol_offsets: [] },
    }
    if (Array.isArray(attachments) && attachments.length) {
      metadata.attachments = attachments.map((a) => ({
        id: a.id,
        mimeType: a.mimeType || 'text/plain',
        name: a.name,
        size: a.size,
      }))
    }
    const body = {
      action: 'next',
      messages: [
        {
          id: messageId,
          author: { role: 'user' },
          create_time: now,
          content: { content_type: 'text', parts: [prompt] },
          metadata,
        },
      ],
      parent_message_id: crypto.randomUUID(),
      model: model || 'auto',
      client_prepare_state: 'sent',
      timezone_offset_min: -480,
      timezone: 'Asia/Shanghai',
      conversation_mode: { kind: 'primary_assistant' },
      enable_message_followups: true,
      system_hints: [],
      supports_buffering: true,
      supported_encodings: ['v1'],
      paragen_cot_summary_display_override: 'allow',
      force_parallel_switch: 'auto',
      client_contextual_info: {
        is_dark_mode: false,
        time_since_loaded: 1200,
        page_height: 1072,
        page_width: 1724,
        pixel_ratio: 1.2,
        screen_height: 1440,
        screen_width: 2560,
        app_name: 'chatgpt.com',
      },
    }
    if (chatSessionId) body.conversation_id = chatSessionId

    console.log('[PROMPT] REQ', {
      chatSessionId,
      parentMessageId,
      prompt,
      promptLength: prompt.length,
      attachments: attachments ? attachments.length : 0,
    })

    const url = `${this.BASE_URL}/backend-api/f/conversation`

    const res = await this._fetch(url, {
      method: 'POST',
      headers: this._buildHeaders({ accept: 'text/event-stream' }, '/backend-api/f/conversation'),
      body: JSON.stringify(body),
    })

    console.log(res.status, res.statusText)

    if (res.status !== 200) {
      console.log(`[ChatGPT] Got ${res.status}`)
    }

    if (!res.ok) {
      const errText = await res.text()
      throw new Error(`ChatGPT error ${res.status}: ${errText.slice(0, 300)}`)
    }

    this._captureResponseHeaders(res)

    return res.body
  }

  // ─── File upload (3-step flow, verified via probe 2026-07-08) ──────
  // Lets long text / images be sent as attachments instead of an inline
  // message (web endpoint has a small per-message length cap → 413).
  //   1) POST /backend-api/files → { file_id, upload_url }
  //   2) PUT upload_url (raw bytes, BROWSER fingerprint headers only —
  //      the signed URL is behind Cloudflare; oai/sentinel/cookie headers
  //      trigger 403 error 1010) → 201
  //   3) POST /backend-api/files/{id}/uploaded → registers, state=ready
  // Returns { id, name, size, mimeType } for use as chatCompletion attachment.
  async uploadFile(buf, { fileName, mimeType = 'text/plain', useCase = 'multimodal' } = {}) {
    if (!this._ready) throw new Error('Not initialized')
    const name = fileName || `${crypto.randomUUID()}.txt`
    const size = buf.length

    // Step 1: request upload URL
    const createRes = await this._fetch(`${this.BASE_URL}/backend-api/files`, {
      method: 'POST',
      headers: this._buildHeaders(
        { accept: '*/*', 'content-type': 'application/json' },
        '/backend-api/files',
      ),
      body: JSON.stringify({
        file_name: name,
        file_size: size,
        use_case: useCase,
        timezone_offset_min: -480,
        reset_rate_limits: false,
        mime_type: mimeType,
        store_in_library: true,
        library_persistence_mode: 'opportunistic',
      }),
    })
    if (!createRes.ok) {
      throw new Error(`file create ${createRes.status}: ${(await createRes.text()).slice(0, 200)}`)
    }
    this._captureResponseHeaders(createRes)
    const created = await createRes.json()
    const fileId = created.file_id
    const uploadUrl = created.upload_url
    if (!fileId || !uploadUrl) {
      throw new Error(`file create missing file_id/upload_url: ${JSON.stringify(created).slice(0, 150)}`)
    }

    // Step 2: PUT bytes to signed URL — browser fingerprint headers ONLY.
    const src = this._headers
    const putRes = await this._fetch(uploadUrl, {
      method: 'PUT',
      headers: {
        'content-type': mimeType,
        'x-ms-blob-type': 'BlockBlob',
        'user-agent': src['user-agent'] || '',
        accept: '*/*',
        'accept-language': src['accept-language'] || 'en-US,en;q=0.9',
        origin: 'https://chatgpt.com',
        referer: 'https://chatgpt.com/',
        'sec-ch-ua': src['sec-ch-ua'] || '',
        'sec-ch-ua-mobile': src['sec-ch-ua-mobile'] || '?0',
        'sec-ch-ua-platform': src['sec-ch-ua-platform'] || '',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'cross-site',
      },
      body: buf,
    })
    if (putRes.status !== 200 && putRes.status !== 201) {
      throw new Error(`file PUT ${putRes.status}: ${(await putRes.text()).slice(0, 150)}`)
    }

    // Step 3: register uploaded
    const regRes = await this._fetch(`${this.BASE_URL}/backend-api/files/${fileId}/uploaded`, {
      method: 'POST',
      headers: this._buildHeaders(
        { accept: '*/*', 'content-type': 'application/json' },
        `/backend-api/files/${fileId}/uploaded`,
      ),
      body: JSON.stringify({}),
    })
    if (!regRes.ok) {
      throw new Error(`file register ${regRes.status}: ${(await regRes.text()).slice(0, 150)}`)
    }
    this._captureResponseHeaders(regRes)

    // Step 4: process/index the file so the model can actually READ its content.
    // store_in_library + library_persistence_mode are required for the model to
    // access the file via retrieval. Without them the file uploads but the model
    // replies "I don't have access to the file".
    const procRes = await this._fetch(`${this.BASE_URL}/backend-api/files/process_upload_stream`, {
      method: 'POST',
      headers: this._buildHeaders(
        { accept: 'text/event-stream', 'content-type': 'application/json' },
        '/backend-api/files/process_upload_stream',
      ),
      body: JSON.stringify({
        file_id: fileId,
        use_case: useCase,
        index_for_retrieval: true,
        file_name: name,
        library_persistence_mode: 'opportunistic',
        metadata: { store_in_library: true, is_temporary_chat: false },
        entry_surface: 'chat_composer',
      }),
    })
    if (procRes.ok) {
      const procText = await procRes.text()
      if (!procText.includes('indexing.completed')) {
        console.log(`[ChatGPT] file indexing ambiguous: ${procText.slice(-150)}`)
      }
    } else {
      console.log(`[ChatGPT] file process ${procRes.status} (continuing)`)
    }

    // Wait briefly for indexing to settle before the conversation references it
    await new Promise((resolve) => setTimeout(resolve, 2000))

    console.log(`[ChatGPT] uploaded file ${fileId} (${size}B) name=${name}`)
    return { id: fileId, name, size, mimeType }
  }

  // ─── Conversation prepare (conduit token refresh) ───────────
  // HAR shows this is called before EVERY /f/conversation POST.
  // First call sends "x-conduit-token: no-token". Subsequent calls
  // send the previously returned conduit_token.

  async _prepareConversation(conversationId, parentMessageId, model = null, attachments = null) {
    const url = `${this.BASE_URL}/backend-api/f/conversation/prepare`
    const body = {
      action: 'next',
      fork_from_shared_post: false,
      parent_message_id: parentMessageId || 'client-created-root',
      model: model || 'auto',
      client_prepare_state: conversationId ? 'success' : 'success',
      timezone_offset_min: -480,
      timezone: 'Asia/Shanghai',
      conversation_mode: { kind: 'primary_assistant' },
      system_hints: [],
      partial_query: {
        id: crypto.randomUUID(),
        author: { role: 'user' },
        content: { content_type: 'text', parts: [''] },
      },
      supports_buffering: true,
      supported_encodings: ['v1'],
      client_contextual_info: { app_name: 'chatgpt.com' },
    }

    if (conversationId) {
      body.conversation_id = conversationId
    }

    if (Array.isArray(attachments) && attachments.length) {
      body.attachments = attachments.map((a) => ({ file_id: a.id }))
    }

    const prepOverrides = {
      accept: '*/*',
      'x-conduit-token': this._headers['x-conduit-token'] || 'no-token',
    }

    const res = await this._fetch(url, {
      method: 'POST',
      headers: this._buildHeaders(
        prepOverrides,
        '/backend-api/f/conversation/prepare',
      ),
      body: JSON.stringify(body),
    })

    if (!res.ok) {
      console.log(`[ChatGPT] Prepare conversation returned ${res.status}`)
      return
    }

    this._captureResponseHeaders(res)

    const data = await res.json()
    if (data.conduit_token) {
      this._headers['x-conduit-token'] = data.conduit_token
      console.log('[ChatGPT] Conduit token from body for conversation:', conversationId)
    }
  }

  // ─── Sentinel refresh ─────────────────────────────────────────

  async _refreshSentinel() {
    if (!this._config) throw new Error('No proof token config for sentinel')

    const sentinelProof = ChatGPTProofOfWork.generateSentinelProof([...this._config])

    const url = `${this.BASE_URL}/backend-api/sentinel/chat-requirements/prepare`
    const res = await this._fetch(url, {
      method: 'POST',
      headers: this._buildHeaders(
        { accept: '*/*', 'content-type': 'application/json' },
        '/backend-api/sentinel/chat-requirements/prepare',
      ),
      body: JSON.stringify({ p: sentinelProof }),
    })

    if (!res.ok) {
      const text = await res.text()
      const err = new Error(`Sentinel ${res.status}: ${text.slice(0, 200)}`)
      err.code = res.status
      throw err
    }

    this._captureResponseHeaders(res)

    const data = await res.json()
    if (!data.prepare_token || !data.proofofwork) {
      throw new Error(`Sentinel unexpected: ${JSON.stringify(data)}`)
    }

    const powProof = ChatGPTProofOfWork.solve(data.proofofwork.seed, data.proofofwork.difficulty, [
      ...this._config,
    ])

    this._headers['openai-sentinel-chat-requirements-prepare-token'] = data.prepare_token
    this._headers['openai-sentinel-proof-token'] = powProof + '~S'
    if (data.turnstile?.dx) {
      this._headers['openai-sentinel-turnstile-token'] = data.turnstile.dx
    }
  }

  // ─── Response header capture ─────────────────────────────────

  _captureResponseHeaders(res) {
    // Capture all cookies via shared CookieJar
    this._cookies.captureFromFetchHeaders(res.headers, ' ChatGPT')

    const oaiIsUpdate = res.headers.get('x-oai-is-update')
    if (oaiIsUpdate) {
      this._headers['x-oai-is'] = oaiIsUpdate
      // console.log('[ChatGPT] Updated x-oai-is from response header')
    }

    const conduitToken = res.headers.get('x-conduit-token')
    if (conduitToken) {
      this._headers['x-conduit-token'] = conduitToken
      // console.log('[ChatGPT] Updated x-conduit-token from response header')
    }

    this._headers['cookie'] = this._cookies.toString()
  }

  // ─── Headers ──────────────────────────────────────────────────
  // Header order matches browser HAR exactly per endpoint.
  // Cloudflare fingerprinting checks header order — must match real browser.

  _buildHeaders(overrides = {}, targetPath = '') {
    const src = this._headers
    const isSentinel = targetPath?.includes('/sentinel/')
    const isPrepare = targetPath?.includes('/conversation/prepare')
    const isConversation = targetPath?.includes('/conversation') && !isPrepare

    // Build ordered list of [name, value] pairs matching HAR line order
    const h = []

    // ── Block 1: Common prefix (all endpoints, exact HAR order) ──
    h.push(['accept', overrides.accept || '*/*'])
    h.push(['accept-encoding', 'gzip, deflate, br, zstd'])
    h.push(['accept-language', src['accept-language'] || 'en-US,en;q=0.9'])
    h.push(['cache-control', 'no-cache'])
    // content-length is set automatically by fetch()
    h.push(['content-type', overrides['content-type'] || 'application/json'])

    // ── Block 9: Cookies from jar (last before overrides) ──
    const cookieStr = this._cookies.toString()
    if (cookieStr) {
      h.push(['cookie', cookieStr])
    }

    h.push(['oai-client-build-number', src['oai-client-build-number'] || ''])
    h.push(['oai-client-version', src['oai-client-version'] || ''])
    h.push(['oai-device-id', src['oai-device-id'] || ''])

    h.push(['oai-language', src['oai-language'] || 'en-US'])
    h.push(['oai-session-id', src['oai-session-id'] || ''])

    // ── Block 3: Conversation-only: sentinel tokens ──
    if (isConversation) {
      if (src['openai-sentinel-chat-requirements-prepare-token']) {
        h.push([
          'openai-sentinel-chat-requirements-prepare-token',
          src['openai-sentinel-chat-requirements-prepare-token'],
        ])
      }
      if (src['openai-sentinel-proof-token']) {
        h.push(['openai-sentinel-proof-token', src['openai-sentinel-proof-token']])
      }
      if (src['openai-sentinel-turnstile-token']) {
        h.push(['openai-sentinel-turnstile-token', src['openai-sentinel-turnstile-token']])
      }
    }

    // ── Block 4: Common continues ──
    h.push(['origin', 'https://chatgpt.com'])
    h.push(['pragma', 'no-cache'])
    h.push(['priority', 'u=1, i'])
    h.push(['referer', src['referer'] || 'https://chatgpt.com/'])
    h.push(['sec-ch-ua', src['sec-ch-ua'] || ''])
    h.push(['sec-ch-ua-mobile', src['sec-ch-ua-mobile'] || '?0'])
    h.push(['sec-ch-ua-platform', src['sec-ch-ua-platform'] || ''])
    h.push(['sec-fetch-dest', 'empty'])
    h.push(['sec-fetch-mode', 'cors'])
    h.push(['sec-fetch-site', 'same-origin'])
    h.push(['user-agent', src['user-agent'] || ''])

    // ── Block 4b: Authorization (required on sentinel + prepare + conversation) ──
    // The sentinel prepare_token is BOUND to whether authorization was present:
    //   sentinel(auth) → prepare_token compatible with auth conversation
    //   sentinel(no-auth) + conversation(auth) → 500 Internal Server Error
    // All three calls must consistently include authorization.
    if (src['authorization'] || src['Authorization']) {
      h.push(['authorization', src['authorization'] || src['Authorization']])
    }

    // ── Block 5: Prepare + conversation: conduit token ──
    if (isPrepare || isConversation) {
      h.push(['x-conduit-token', src['x-conduit-token'] || 'no-token'])
    }

    // ── Block 6: x-oai-is (all authenticated) ──
    if (src['x-oai-is']) {
      h.push(['x-oai-is', src['x-oai-is']])
    }

    // ── Block 7: trace-id (prepare + conversation, NOT sentinel) ──
    if (isPrepare || isConversation) {
      h.push(['x-oai-turn-trace-id', src['x-oai-turn-trace-id'] || ''])
    }

    // ── Block 8: Target path/route (all endpoints) ──
    if (targetPath) {
      h.push(['x-openai-target-path', targetPath])
      h.push(['x-openai-target-route', targetPath])
    }

    // Convert ordered pairs to object (JS preserves insertion order)
    const base = {}
    for (const [k, v] of h) {
      base[k] = v
    }

    // Apply remaining overrides (except those already consumed)
    const extra = { ...overrides }
    delete extra.accept
    delete extra['content-type']
    Object.assign(base, extra)

    return base
  }

  async _fetch(url, options = {}) {
    return fetch(url, { ...options, redirect: 'follow' })
  }

  isReady() {
    return this._ready
  }
}

module.exports = { ChatGPTAPI }
