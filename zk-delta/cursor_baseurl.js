'use strict'
/*
 * zk-delta/cursor_baseurl.js —— 只改 Cursor 的 BYOK 地址，一个字段，别的一律不动。
 *
 * 为什么不复用 scripts/zk-cursor-web/cursor_team_setup.js：
 *   那是**装机安装器**，--apply 会顺手做四件我这里不想要的事——重打 app bundle 补丁、
 *   往 userAddedModels 里塞 6 个 cursor-g 模型、提示粘 API Key，以及最要命的一条：
 *   mergeConfig() 里当选中的模型名不以 "cursor-g" 开头时，会把 composer / cmd-k 的
 *   modelName 覆盖成 cursor-g-5.6-sol。而本机 composer 现在正是基准
 *   cursor-web-fc-82-terra —— 拿安装器当开关用，会把门①门② 的基准模型悄悄换掉。
 *   切地址和装机是两件事，不该共用一个入口。
 *
 * 不变量（写完当场验，不成立就还原并退非零）：
 *   除 openAIBaseUrl 外，blob 里其余字段一个字节都不许变。
 *
 * 用法：
 *   node zk-delta/cursor_baseurl.js get
 *   node zk-delta/cursor_baseurl.js set https://cc.auto-link.com.cn/pro/v1
 */

const fs = require('fs')
const path = require('path')
const os = require('os')
const { execFileSync } = require('child_process')

const STATE_DB = path.join(os.homedir(),
  'Library/Application Support/Cursor/User/globalStorage/state.vscdb')
const KEY = 'src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl' +
  '.persistentStorage.applicationUser'
const BACKUP_DIR = path.join(os.homedir(), '.zk-delta-backup')

function die (msg, code) { console.error('!! ' + msg); process.exit(code || 1) }

function openDb () {
  const { DatabaseSync } = require('node:sqlite')
  const db = new DatabaseSync(STATE_DB)
  return {
    get: (k) => { const r = db.prepare('SELECT value FROM ItemTable WHERE key=?').get(k); return r ? r.value : null },
    set: (k, v) => db.prepare(
      'INSERT INTO ItemTable(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(k, v),
    close: () => db.close()
  }
}

function cursorRunning () {
  try { execFileSync('pgrep', ['-x', 'Cursor'], { stdio: 'ignore' }); return true } catch (e) { return false }
}

function readBlob () {
  if (!fs.existsSync(STATE_DB)) die('找不到 Cursor 状态库: ' + STATE_DB, 3)
  const db = openDb()
  const raw = db.get(KEY)
  db.close()
  if (!raw) die('applicationUser blob 不存在（Cursor 没初始化过？）', 3)
  return raw
}

function summarize (d) {
  const mc = (d.aiSettings && d.aiSettings.modelConfig) || {}
  return {
    baseUrl: d.openAIBaseUrl,
    useOpenAIKey: d.useOpenAIKey,
    composer: mc.composer && mc.composer.modelName,
    cmdk: mc['cmd-k'] && mc['cmd-k'].modelName
  }
}

const cmd = process.argv[2] || 'get'

if (cmd === 'get') {
  const s = summarize(JSON.parse(readBlob()))
  console.log(JSON.stringify(s, null, 2))
  process.exit(0)
}

if (cmd !== 'set') die('用法: node cursor_baseurl.js {get|set <url>}', 2)

const want = process.argv[3]
if (!want || !/^https?:\/\//.test(want)) die('set 需要一个 http(s) 地址', 2)

// 外部写 state.vscdb 有内存覆盖竞态：Cursor 开着改了也会被它写回去。
if (!process.env.ZKD_SKIP_RUNNING_CHECK && cursorRunning()) {
  die('Cursor 正在运行 —— 先完全退出（⌘Q）再跑，否则改了会被它的内存覆盖回去。', 2)
}

const rawBefore = readBlob()
const before = JSON.parse(rawBefore)

if (before.openAIBaseUrl === want) {
  console.log('   已经是 ' + want + '，不用改。')
  process.exit(0)
}

fs.mkdirSync(BACKUP_DIR, { recursive: true })
const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)
const bak = path.join(BACKUP_DIR, `applicationUser.${stamp}.json`)
fs.writeFileSync(bak, rawBefore)
console.log('   备份原 blob -> ' + bak + ' (' + rawBefore.length + ' B)')

const next = JSON.parse(rawBefore)
next.openAIBaseUrl = want

const db = openDb()
db.set(KEY, JSON.stringify(next))
db.close()

// 读回来验，而不是"写了就当成了"
const rawAfter = readBlob()
const after = JSON.parse(rawAfter)

if (after.openAIBaseUrl !== want) {
  fs.writeFileSync(path.join(BACKUP_DIR, 'FAILED-readback.json'), rawAfter)
  die('读回来还是 ' + JSON.stringify(after.openAIBaseUrl) + '，没写进去。原 blob 在 ' + bak, 4)
}

// 不变量：把地址换回旧值之后，两边必须完全相同 —— 也就是我只动了这一个字段。
const probe = JSON.parse(rawAfter)
probe.openAIBaseUrl = before.openAIBaseUrl
if (JSON.stringify(probe) !== JSON.stringify(before)) {
  const dbr = openDb(); dbr.set(KEY, rawBefore); dbr.close()
  die('除地址外还有别的字段变了，已还原成备份。这条不许放过。', 5)
}

const a = summarize(before)
const b = summarize(after)
console.log('   openAIBaseUrl ' + JSON.stringify(a.baseUrl) + ' -> ' + JSON.stringify(b.baseUrl))
console.log('   其余不动：useOpenAIKey=' + b.useOpenAIKey +
  '  composer=' + JSON.stringify(b.composer) + '  cmd-k=' + JSON.stringify(b.cmdk))
