// 测试用:把**仓库里真的** litellm-key-poller.js 原样加载(不改一个字),只把 https 换成假的,
// 跑 generateKey(masterKey, 'cursor', emailPrefix, identity),把它发给 /key/generate 的 body 打到 stdout。
// 用法:node poller_harness.js <poller.js 路径>  ,stdin = {now, email_prefix, identity, reference, teams}
// 只被 test_issue.py 调;不连任何网络。
const fs = require('fs');
const Module = require('module');
const path = require('path');

const pollerPath = path.resolve(process.argv[2]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const seen = { generate: null, paths: [] };

const fakeHttps = {
  request(opts, cb) {
    const chunks = [];
    const req = {
      setTimeout() {}, on() {},
      write(b) { chunks.push(b); },
      end() {
        const p = opts.path;
        seen.paths.push(opts.method + ' ' + p);
        let out;
        if (p.includes('/key/list')) out = { keys: [input.reference], total_pages: 1 };
        else if (p.endsWith('/team/list')) out = input.teams;
        else if (p.endsWith('/key/generate')) { seen.generate = JSON.parse(chunks.join('')); out = { key: 'sk-harness-not-real-0000' }; }
        else out = { error: 'harness: no route ' + p };
        const res = { on(ev, f) { if (ev === 'data') f(JSON.stringify(out)); if (ev === 'end') f(); } };
        cb(res);
      },
    };
    return req;
  },
};

// 原样读源码,只在末尾多导出两个函数(module.exports 本来没导出 generateKey)
const src = fs.readFileSync(pollerPath, 'utf8') + '\nmodule.exports.__generateKey = generateKey;\n';
const m = new Module(pollerPath, null);
m.filename = pollerPath;
m.paths = Module._nodeModulePaths(path.dirname(pollerPath));
const realRequire = m.require.bind(m);
m.require = (id) => (id === 'https' ? fakeHttps : realRequire(id));
Date.now = () => input.now;
m._compile(src, pollerPath);

(async () => {
  const ident = input.identity;
  // = poller main() :998-1002:identity 来自 resolveApplicantIdentity,再补 recordId / teamId
  ident.teamId = await m.exports.findDepartmentTeamId('mk-harness', ident.department);
  await m.exports.__generateKey('mk-harness', 'cursor', input.email_prefix, ident);
  process.stdout.write(JSON.stringify({ payload: seen.generate, team_id: ident.teamId, paths: seen.paths }));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
