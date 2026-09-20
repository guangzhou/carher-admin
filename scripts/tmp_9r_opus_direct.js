// Direct probe against 9router, bypassing LiteLLM entirely, so the 500 can be
// attributed to one layer rather than to "somewhere in the chain".
// Legs: opus-5-medium (the failing one) x3, then fable-5-1-medium (the working
// one) as a positive control in the SAME run, so a red on opus with a green on
// fable rules out "9router is just down right now".
const http = require('http');
const Database = require('/app/node_modules/better-sqlite3');
const db = new Database('/app/data/db/data.sqlite', { readonly: true });
const KEY = db.prepare("select key from apiKeys where isActive=1 limit 1").get().key;

function ask(model) {
  return new Promise((resolve) => {
    const body = JSON.stringify({
      model,
      messages: [{ role: 'user', content: 'Reply with exactly: DIRECT-OK' }],
      max_tokens: 32,
    });
    const t0 = Date.now();
    const req = http.request({
      host: '127.0.0.1', port: 20128, path: '/v1/chat/completions', method: 'POST',
      headers: {
        Authorization: 'Bearer ' + KEY,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
    }, (res) => {
      let b = '';
      res.on('data', (d) => (b += d));
      res.on('end', () => {
        let txt = b.slice(0, 300);
        try {
          const j = JSON.parse(b);
          txt = j.choices?.[0]?.message?.content ?? JSON.stringify(j).slice(0, 300);
        } catch (e) { /* keep raw */ }
        resolve({ model, status: res.statusCode, ms: Date.now() - t0, txt });
      });
    });
    req.setTimeout(240000, () => { req.destroy(); resolve({ model, status: 'TIMEOUT', ms: Date.now() - t0, txt: '' }); });
    req.on('error', (e) => resolve({ model, status: 'ERR', ms: Date.now() - t0, txt: e.message }));
    req.write(body);
    req.end();
  });
}

(async () => {
  const legs = [
    'cu/claude-opus-5-medium',
    'cu/claude-opus-5-medium',
    'cu/claude-opus-5-medium',
    'cu/claude-fable-5-1-medium',   // positive control, same run
    'cu/claude-opus-5-low',         // is it the whole opus-5 family or just medium?
    'cu/claude-opus-5-high',
  ];
  for (const m of legs) {
    const r = await ask(m);
    console.log(`[direct] ${r.model.padEnd(30)} status=${String(r.status).padEnd(7)} ${String(r.ms).padStart(6)}ms  ${JSON.stringify(r.txt).slice(0, 200)}`);
  }
})();
