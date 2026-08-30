const fs = require('fs')
const code = fs.readFileSync('/tmp/r.js', 'utf8')
let pass=0, fail=0
function check(n, ok, why){ if(ok){pass++;console.log('  PASS '+n)}else{fail++;console.log('  FAIL '+n+' — '+(why||''))} }

check('S0 IDE strip regex 存在', code.includes('<open_and_recently_viewed_files>[\\s\\S]*?<\\/open_and_recently_viewed_files>'))
check('S0b [ide-strip] log 存在', code.includes('[ide-strip] removed'))
check('S0c 无门控(默认剥)', !/ZK_STRIP_IDE|ZK_IDE_STRIP/.test(code))

const mFn = code.match(/const _DIET_KEEP_RES = [\s\S]*?function dietCursorInput\(input\) \{[\s\S]*?\n\}/)
check('S1 dietCursorInput 可抠', !!mFn)

const src = 'function textOfContent(c){return typeof c==="string"?c:(Array.isArray(c)?c.map(x=>x.text||"").join(""):"")}\n' + mFn[0] + '\nreturn dietCursorInput'
const fn = new Function('process','console', src)
const sandbox = { process:{env:{}}, console:{log:()=>{}} }
const diet = fn(sandbox.process, sandbox.console)

const input1 = [{ type:'message', content:"<open_and_recently_viewed_files>\nFiles that are currently open and visible in the user's IDE:\n- /path/to/foo.md (total lines: 182)\n</open_and_recently_viewed_files>快速排序" }]
const out1 = diet(input1)
check('S2 用户实锤形状剥完', out1[0].content === '快速排序', 'got: ' + JSON.stringify(out1[0].content))

const input2 = [{ type:'message', content:'普通问题,你好' }]
check('S3 无标签消息不动', diet(input2)[0].content === '普通问题,你好')

const input3 = [{ type:'message', content:'前缀\n<open_and_recently_viewed_files>foo</open_and_recently_viewed_files>\n真正问题' }]
const out3 = diet(input3)
check('S4 中间标签也剥', !out3[0].content.includes('open_and_recently') && out3[0].content.includes('真正问题'))

const input4 = [{ type:'message', content:'<open_and_recently_viewed_files>a</open_and_recently_viewed_files>A<open_and_recently_viewed_files>b</open_and_recently_viewed_files>B' }]
check('S5 多标签全剥', diet(input4)[0].content === 'AB')

const bigFramework = '<user_info>uid=1</user_info>\n<mcp_server_catalog>cat</mcp_server_catalog>\n<user_query>问</user_query>\n' + 'x'.repeat(5000)
const input5 = [{ type:'message', content: '<open_and_recently_viewed_files>drop me</open_and_recently_viewed_files>\n' + bigFramework }]
const out5 = diet(input5)
check('S6 IDE 剥后大框架仍走 diet',
  !out5[0].content.includes('open_and_recently') && out5[0].content.includes('<user_info>') && out5[0].content.includes('<user_query>'))

const input6 = [{ type:'function_call_output', output:'<open_and_recently_viewed_files>x</open_and_recently_viewed_files>keep' }]
check('S7 非 message 不动', diet(input6)[0].output.includes('open_and_recently'))

sandbox.process.env.ZK_DIET = '0'
check('S8 ZK_DIET=0 时不跑,IDE 保留', diet(input1)[0].content.includes('open_and_recently'))

console.log(`\n${pass} pass / ${fail} fail`)
if (fail) process.exit(1)
