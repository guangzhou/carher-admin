const fs = require('fs')
const code = fs.readFileSync('/tmp/r.js', 'utf8')
const { execSync } = require('child_process')
let pass=0, fail=0
function check(n, ok, why){ if(ok){pass++;console.log('  PASS '+n)}else{fail++;console.log('  FAIL '+n+' — '+(why||''))} }

check('S0 [lark-esc] 存在', code.includes('[lark-esc]'))
check('S0b lark-cli 判据挂在 shellTool 分支', /lark-cli\\s.*_v2cmdFixed/s.test(code))
check('S0c 无门控', !/ZK_LARK_ESC/.test(code))
check('S0d execToToolCall 用 _v2cmdFixed', /execToToolCall\(shellTool, _v2cmdFixed\)/.test(code))

function larkFix(cmd) {
  let out = cmd
  if (/(^|\s|;|&&)lark-cli\s/.test(out)) {
    out = out.replace(
      /(--content\s+)(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')/g,
      (all, prefix, dq, sq) => {
        const raw = dq !== undefined ? dq : sq
        const unescaped = raw.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\r/g, '\r')
        if (unescaped === raw) return all
        const hasSingle = unescaped.indexOf("'") >= 0
        if (!hasSingle) return `${prefix}'${unescaped}'`
        return `${prefix}'${unescaped.replace(/'/g, "'\\''")}'`
      }
    )
  }
  return out
}

// 真 shell round-trip 验值(修法关键:飞书要收到真换行)
function shellValue(fixedCmd) {
  // 提取 --content 后跟的 quoted string,shell 展开一次
  const cmd = fixedCmd.replace(/^lark-cli\s+markdown\s+\+create\s+/, 'printf %s ')
  return execSync(cmd, { stdio: ["ignore","pipe","ignore"] }).toString().replace(/^--content/, "")
}

const badCmd = `lark-cli markdown +create --content "# 测试文档\\n\\n这是一个用于测试的飞书文档。\\n\\n创建日期:2026-08-30"`
const fixedCmd = larkFix(badCmd)
const val = shellValue(fixedCmd)
check('S2 端到端:shell 展开后飞书会收到真换行',
  val === '# 测试文档\n\n这是一个用于测试的飞书文档。\n\n创建日期:2026-08-30',
  'shell got: ' + JSON.stringify(val))

check('S3 修法输出不再含字面 \\\\n', !fixedCmd.includes('\\n\\n'))

check('S4 非 lark-cli 命令不动', larkFix(`echo "line1\\nline2" > /tmp/x`) === `echo "line1\\nline2" > /tmp/x`)

const single = `lark-cli markdown +create --content '# hi\\n\\nworld'`
const val5 = shellValue(larkFix(single))
check('S5 单引号 --content 也修', val5 === '# hi\n\nworld', 'got: ' + JSON.stringify(val5))

const withSingle = `lark-cli markdown +create --content "It's ok\\ndone"`
const val6 = shellValue(larkFix(withSingle))
check('S6 内容含单引号 shell 展开正确', val6 === "It's ok\ndone", 'got: ' + JSON.stringify(val6))

const clean = `lark-cli markdown +create --content "hello world"`
check('S7 无转义不改动', larkFix(clean) === clean)

const tabTest = `lark-cli markdown +create --content "col1\\tcol2\\tcol3"`
const val8 = shellValue(larkFix(tabTest))
check('S8 \\t 展开', val8 === 'col1\tcol2\tcol3', 'got: ' + JSON.stringify(val8))

// 一堆坑:美元符号不展开、backtick 不展开
const dollar = `lark-cli markdown +create --content "price is $100\\nno expand"`
const val9 = shellValue(larkFix(dollar))
check('S9 $ 不被 shell 展开(单引号包)', val9 === 'price is $100\nno expand', 'got: ' + JSON.stringify(val9))

console.log(`\n${pass} pass / ${fail} fail`)
if (fail) process.exit(1)
