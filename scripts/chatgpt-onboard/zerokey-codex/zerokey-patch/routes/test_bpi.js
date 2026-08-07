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
console.log(`\n=== 编译器 ${pass} passed, ${fail} failed ===`)

// ── 入站改造 ──
const P=require('./bpi-codex.js')
console.log('\n-- 入站改造 --')
let p2=0,f2=0
function t2(n,fn){try{fn();p2++;console.log('  ✅',n)}catch(e){f2++;console.log('  ❌',n,'\n     ',e.message)}}
const codexInput=[
  {type:'additional_tools',role:'developer',tools:[{type:'custom',name:'exec'}]},
  {type:'message',role:'developer',content:[{type:'input_text',text:'<permissions instructions> sandbox_mode ...'}]},
  {type:'message',role:'user',content:[{type:'input_text',text:'# AGENTS.md instructions for /Users/lgx/codes/carher-admin\n<INSTRUCTIONS>...'}]},
  {type:'message',role:'user',content:[{type:'input_text',text:'ls'}]},
]
t2('只剥环境说明，保留 agent 身份提示词',()=>{
  const inp=[...codexInput,
    {type:'message',role:'developer',content:[{type:'input_text',
      text:'You are Codex, an agent based on GPT-5. You and the user share one workspace...'}]}]
  const out=P.prepareCodexInput(inp)
  assert.ok(!out.some(i=>i.type==='additional_tools'),'additional_tools 要剥')
  const devs=out.filter(i=>i.role==='developer').map(i=>i.content[0].text)
  assert.ok(devs.some(t=>t.startsWith('You are Codex')),'agent 提示词必须保留')
  assert.ok(!devs.some(t=>t.includes('<permissions instructions>')),'环境说明必须剥掉')
})
t2('sandbox_mode 那种写法也认得出来',()=>{
  assert.ok(P.isEnvironmentPrompt('<permissions instructions>\nFilesystem...'))
  assert.ok(P.isEnvironmentPrompt('Note: `sandbox_mode` is `danger-full-access`'))
  assert.ok(!P.isEnvironmentPrompt('You are Codex, an agent based on GPT-5.'))
})
t2('AGENTS.md（user 角色）必须保留',()=>{
  const out=P.prepareCodexInput(codexInput)
  assert.ok(JSON.stringify(out).includes('AGENTS.md instructions'),'AGENTS.md 被误删')
})
t2('工作目录能从 AGENTS.md 抬头抽出来',()=>{
  assert.equal(P.extractCwd(codexInput),'/Users/lgx/codes/carher-admin')
})
t2('末尾追加交手块，且带上 cwd',()=>{
  const out=P.prepareCodexInput(codexInput)
  const last=out[out.length-1].content[0].text
  assert.ok(last.includes('[本轮可用的手]'))
  assert.ok(last.includes('/Users/lgx/codes/carher-admin'))
  assert.ok(last.includes('不要用内建的代码解释器'),'必须明确禁止伸自己的沙箱')
})
t2('非 Codex 载荷原样返回（Cursor 等零影响）',()=>{
  const plain=[{type:'message',role:'user',content:[{type:'input_text',text:'hi'}]}]
  assert.strictEqual(P.prepareCodexInput(plain),plain,'应返回同一个对象引用')
})
t2('没有 AGENTS.md 时不写路径，也不崩',()=>{
  const noagents=[{type:'additional_tools',role:'developer',tools:[]},
                  {type:'message',role:'user',content:[{type:'input_text',text:'hi'}]}]
  const out=P.prepareCodexInput(noagents)
  const last=out[out.length-1].content[0].text
  assert.ok(last.includes('[本轮可用的手]'))
  assert.ok(!last.includes('当前工作目录'))
})
console.log(`\n=== 入站 ${p2} passed, ${f2} failed ===`)

console.log('\n-- 拒答检测 / ask 映射 --')
let p3=0,f3=0
function t3(n,fn){try{fn();p3++;console.log('  ✅',n)}catch(e){f3++;console.log('  ❌',n,'\n     ',e.message)}}
t3('用户实际遇到的三句拒答都要认出来',()=>{
  for(const s of ['我这里当前没有可用的终端执行权限，不能直接运行 ls 查看目录。',
                  '当前对话环境里我仍没有可调用的本地终端执行接口',
                  '无法访问当前工作目录 /Users/x 执行 ls（运行环境中该路径不可用，命令未执行成功）'])
    assert.ok(P.needsEscalation(s),'漏判: '+s.slice(0,20))
})
t3('已经吐块了就不重试',()=>{
  assert.ok(!P.needsEscalation('⟦ls¦path=/tmp⟧'))
  assert.ok(!P.needsEscalation('⟦write¦path=/tmp/a¦content=x⟧ 我没有权限'))
})
t3('正常回答不误判',()=>{
  assert.ok(!P.needsEscalation('快速排序的平均复杂度是 O(n log n)。'))
  assert.ok(!P.needsEscalation(''))
})
t3('ask -> request_user_input，schema 对得上',()=>{
  const fc=P.firstAsk('⟦ask¦question=要用哪个路径？¦option=/tmp/a.txt¦option=其他⟧')
  assert.equal(fc.name,'request_user_input')
  const q=fc.arguments.questions[0]
  assert.equal(q.question,'要用哪个路径？')
  assert.ok(q.header.length<=12,'header 要 <=12 字符')
  assert.equal(q.options.length,2)
})
t3('只有一个 option 时不塞 options（schema 要求 2-3 个）',()=>{
  const fc=P.firstAsk('⟦ask¦question=继续吗？¦option=好⟧')
  assert.ok(!('options' in fc.arguments.questions[0]))
})
console.log(`\n=== 拒答/ask ${p3} passed, ${f3} failed ===`)

console.log('\n-- 经 LiteLLM 改写后的形态 --')
let p4=0,f4=0
function t4(n,fn){try{fn();p4++;console.log('  ✅',n)}catch(e){f4++;console.log('  ❌',n,'\n     ',e.message)}}
t4('normalize 把 developer 改成 system 且删掉 type，仍要能剥掉',()=>{
  const afterNormalize=[
    {type:'additional_tools',role:'developer',tools:[]},
    {role:'system',content:'<permissions instructions>\nFilesystem sandboxing... `sandbox_mode` is `danger-full-access`'},
    {role:'system',content:'You are Codex, an agent based on GPT-5.'},
    {type:'message',role:'user',content:[{type:'input_text',text:'ls'}]},
  ]
  const out=P.prepareCodexInput(afterNormalize)
  const txt=JSON.stringify(out)
  assert.ok(!txt.includes('permissions instructions'),'环境说明没被剥掉（这就是经 LiteLLM 时失效的原因）')
  assert.ok(txt.includes('You are Codex'),'agent 提示词被误删')
})
t4('content 是纯字符串时 textOf 不炸',()=>{
  assert.ok(P.isEnvironmentPrompt('<permissions instructions> x'))
})
console.log(`\n=== LiteLLM 形态 ${p4} passed, ${f4} failed ===`)
process.exit(fail+f2+f3+f4?1:0)
