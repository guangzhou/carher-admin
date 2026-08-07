const assert=require('assert')
const B=require('./bpi-codex.js')
let pass=0,fail=0
function t(name,fn){try{fn();pass++;console.log('  ✅',name)}catch(e){fail++;console.log('  ❌',name,'\n     ',e.message)}}

t('实测模型输出：ls + glob 两块',()=>{
  const s='⟦ls¦path=/Users/Liuguoxian/codes/carher-admin⟧\n⟦glob¦pattern=/Users/Liuguoxian/codes/carher-admin/*¦max=200⟧'
  const r=B.compileToExec(s)
  assert.equal(r.blocks.length,2)
  assert.ok(r.js.includes('tools.exec_command'),'应生成 exec_command')
  assert.ok(r.js.includes('ls -la'),'ls 要变成 ls -la')
  assert.equal(r.leftover,'','块之外不该有残留')
})
t('write -> apply_patch 的 Add File 信封',()=>{
  const r=B.compileToExec('⟦write¦path=/tmp/a.md¦content=# 标题\n正文⟧')
  assert.ok(r.js.includes('tools.apply_patch'))
  const m=r.js.match(/apply_patch\((".*?")\)/s); const patch=JSON.parse(m[1])
  assert.ok(patch.startsWith('*** Begin Patch\n*** Add File: /tmp/a.md'),patch.slice(0,60))
  assert.ok(patch.includes('\n+# 标题\n+正文'),'每行要有 + 前缀')
  assert.ok(patch.endsWith('*** End Patch'))
})
t('replace -> Update File',()=>{
  const r=B.compileToExec('⟦replace¦path=/tmp/a.js¦old=foo¦new=bar⟧')
  const patch=JSON.parse(r.js.match(/apply_patch\((".*?")\)/s)[1])
  assert.ok(patch.includes('*** Update File: /tmp/a.js'))
  assert.ok(patch.includes('\n-foo')&&patch.includes('\n+bar'))
})
t('路径里的单引号要转义，不能破 shell',()=>{
  const r=B.compileToExec("⟦ls¦path=/tmp/it's here⟧")
  const cmd=JSON.parse(r.js.match(/\{cmd: (".*?")\}/s)[1])
  assert.ok(cmd.includes("'\\''"),'应有 POSIX 单引号转义: '+cmd)
})
t('反引号包裹的是示例，不编译',()=>{
  const r=B.compileToExec('参考写法：`⟦write¦path=/tmp/x¦content=y⟧`')
  assert.equal(r.js,null); assert.equal(r.blocks.length,0)
})
t('纯文本原样透传（未开契约的号不受影响）',()=>{
  const s='快速排序是一种分治算法。'
  const r=B.compileToExec(s)
  assert.equal(r.js,null); assert.equal(r.leftover,s)
})
t('ask/todos 不翻译成 shell',()=>{
  const r=B.compileToExec('⟦ask¦question=要继续吗¦option=是⟧')
  assert.equal(r.js,null,'交互类块不该被硬翻成命令')
})
t('缺必需参数的块跳过而不是生成坏 JS',()=>{
  assert.equal(B.blockToJs({name:'write',params:{path:'/tmp/a'}}),null)
  assert.equal(B.blockToJs({name:'cmd',params:{}}),null)
})
t('read 带行号 -> sed',()=>{
  const r=B.compileToExec('⟦read¦path=/tmp/a¦from=10¦to=20⟧')
  assert.ok(JSON.parse(r.js.match(/\{cmd: (".*?")\}/s)[1]).startsWith('sed -n 10,20p'))
})
t('生成的 JS 语法合法',()=>{
  const r=B.compileToExec('⟦cmd¦run=echo "hi" && ls⟧\n⟦write¦path=/tmp/b¦content=x⟧')
  new Function('tools','text','return (async()=>{'+r.js+'})')  // 只做语法解析
})
console.log(`\n=== ${pass} passed, ${fail} failed ===`)
process.exit(fail?1:0)
