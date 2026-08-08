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
t3('印刷体撇号必须认出来（2026-08-08 漏判现场）',()=>{
  // 模型用的是 U+2019，不是 ASCII '。写 don't 匹配不上，重试就不会点火。
  const real="I can draft the AGENTS.md content, but I don\u2019t have an active "+
             "file-editing tool connection in this chat to inspect /Users/x or create the file safely."
  assert.ok(P.needsEscalation(real),'印刷体撇号漏判 —— 这正是线上那次直接把拒答返回给用户的原因')
  assert.ok(P.needsEscalation("I don't have access to your filesystem"),'ASCII 撇号也要认')
  assert.ok(P.needsEscalation('unable to access the repo'))
  assert.ok(P.needsEscalation('I cannot write files'))
})
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
t3('措辞无关判据：没吐块却提到绝对路径 -> 重试',()=>{
  // 2026-08-08：修了 don’t 之后又冒出 couldn’t access、以及"I checked for
  // AGENTS.md at /Users/… but…"这种声称查过其实没调工具的。追词追不完。
  assert.ok(P.needsEscalation('I checked for AGENTS.md at /Users/x/codes/y/AGENTS.md, but that path is unreachable'))
  assert.ok(P.needsEscalation('目录 /home/user/proj 下有 3 个文件'))
  assert.ok(P.ABS_PATH_RE.test('/tmp/a.txt'))
})
t3('纯问答不会被误判成要重试',()=>{
  for (const s of ['快速排序的平均复杂度是 O(n log n)。',
                   'AGENTS.md 已存在，按要求未覆盖。',
                   'ls 用于列出目录内容。'])
    assert.ok(!P.needsEscalation(s), '误判: '+s)
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

console.log('\n-- 回程（工具结果拼回 prompt）--')
let p5=0,f5=0
function t5(n,fn){try{fn();p5++;console.log('  ✅',n)}catch(e){f5++;console.log('  ❌',n,'\n     ',e.message)}}
// 用户真实会话第 16 行的 output 原样
const realOut={type:'custom_tool_call_output',call_id:'c1',output:[
  {type:'input_text',text:'Script completed\nWall time 0.2 seconds\nOutput:\n'},
  {type:'input_text',text:'BPI(ls):'},
  {type:'input_text',text:'{"chunk_id":"ca0b05","wall_time_seconds":0.0000019,"exit_code":0,"original_token_count":1180,"output":"total 512\\ndrwxr-xr-x 63 Liuguoxian staff 2016 Aug 7 11:07 .\\ndrwxr-xr-x 21 Liuguoxian staff 672 .."}'}]}
t5('工具结果能被提取成模型看得懂的文本（这就是之前丢掉的东西）',()=>{
  const r=P.replayItemToText(realOut)
  assert.equal(r.role,'user')
  assert.ok(r.text.includes('total 512'),'真正的 stdout 必须出现: '+r.text.slice(0,80))
  assert.ok(r.text.includes('drwxr-xr-x'),'目录内容必须出现')
  assert.ok(!r.text.includes('chunk_id'),'chunk_id 这类噪声不该喂给模型')
  assert.ok(!r.text.includes('Wall time'),'Wall time 也是噪声')
})
t5('调用项回放成块的形状',()=>{
  const r=P.replayItemToText({type:'custom_tool_call',name:'exec',
    input:'text("BPI(ls):"); text(await tools.exec_command({cmd:"ls"}));'})
  assert.equal(r.role,'assistant'); assert.ok(r.text.includes('⟦ls'))
})
t5('普通 item 不动',()=>{
  assert.equal(P.replayItemToText({type:'message',role:'user',content:[]}),null)
})
t5('端到端：带工具结果的 input 走完 prepareCodexInput 后结果还在',()=>{
  const inp=[{type:'additional_tools',role:'developer',tools:[]},
    {type:'message',role:'user',content:[{type:'input_text',text:'ls'}]},
    {type:'custom_tool_call',name:'exec',input:'text("BPI(ls):");'},
    realOut]
  const out=P.prepareCodexInput(inp)
  const flat=JSON.stringify(out)
  assert.ok(flat.includes('total 512'),'回程内容丢了 —— 这正是线上那个 bug')
  assert.ok(!out.some(i=>i.type==='custom_tool_call_output'),'原始 item 应已被替换')
})
t5('空输出不炸',()=>{
  const r=P.replayItemToText({type:'custom_tool_call_output',output:[]})
  assert.ok(r.text.includes('无输出'))
})
console.log(`\n=== 回程 ${p5} passed, ${f5} failed ===`)

console.log('\n-- 拿到结果后要肯讲 --')
let p6=0,f6=0
function t6(n,fn){try{fn();p6++;console.log('  ✅',n)}catch(e){f6++;console.log('  ❌',n,'\n     ',e.message)}}
t6('交手提示词里"只输出块"必须带条件，不能是无条件的',()=>{
  const h=P.handsBlock('/tmp/x')
  assert.ok(h.includes('还需要动手'),'要显式区分"还没动手"')
  assert.ok(h.includes(P.RESULT_HEAD),'要显式提到"已有结果"的情形')
  assert.ok(h.includes('不要再输出块'),'有结果时要禁止继续吐块')
  assert.ok(/ll|ls/.test(h),'要点明 ll/ls 这类简写是让它执行')
})
t6('回程结果带抬头和"据此回答"的指示',()=>{
  const r=P.replayItemToText({type:'custom_tool_call_output',output:[
    {type:'input_text',text:'{"exit_code":0,"output":"a.txt\\nb.txt"}'}]})
  assert.ok(r.text.startsWith(P.RESULT_HEAD),'抬头要和提示词里那个常量一致')
  assert.ok(r.text.includes('a.txt'))
  assert.ok(r.text.includes('直接据此回答'),'要明确要求它讲出来')
})
console.log(`\n=== 讲结果 ${p6} passed, ${f6} failed ===`)

console.log('\n-- 精简重试 prompt --')
let p7=0,f7=0
function t7(n,fn){try{fn();p7++;console.log('  ✅',n)}catch(e){f7++;console.log('  ❌',n,'\n     ',e.message)}}
const bigItems=[{type:'additional_tools',role:'developer',tools:[]},
 {type:'message',role:'developer',content:[{type:'input_text',text:'You are Codex, an agent based on GPT-5. '.repeat(500)}]},
 {type:'message',role:'developer',content:[{type:'input_text',text:'<permissions instructions> sandbox stuff'}]},
 {type:'message',role:'user',content:[{type:'input_text',text:'# AGENTS.md instructions for /Users/lgx/codes/repo\n'+'x'.repeat(9000)}]},
 {type:'message',role:'user',content:[{type:'input_text',text:'把结构写到 /tmp/a.md'}]}]
t7('体积砍到原来的百分之几（重试不该跟首轮一样贵）',()=>{
  const esc=P.escalatePrompt(bigItems,'RETRY')
  assert.ok(esc.length<2000,'重试 prompt 还是太大: '+esc.length)
})
t7('保留必要三件：交手块 / 工作目录 / 用户最后诉求',()=>{
  const esc=P.escalatePrompt(bigItems,'RETRY')
  assert.ok(esc.includes('[本轮可用的手]'))
  assert.ok(esc.includes('/Users/lgx/codes/repo'),'工作目录丢了模型会瞎猜路径')
  assert.ok(esc.includes('把结构写到 /tmp/a.md'))
  assert.ok(esc.includes('RETRY'))
})
t7('剔除打架的上下文（Codex 自己的 agent 提示词 / sandbox 说明）',()=>{
  const esc=P.escalatePrompt(bigItems,'RETRY')
  assert.ok(!esc.includes('You are Codex'))
  assert.ok(!esc.includes('permissions instructions'))
})
const REAL_ASK='真正的诉求：把 src 目录下所有 handler 罗列出来并写进 /tmp/h.md'
t7('不把我们自己塞的块当成用户诉求',()=>{
  const items=[{type:'additional_tools',role:'developer',tools:[]},
    {type:'message',role:'user',content:[{type:'input_text',text:REAL_ASK}]},
    {type:'message',role:'user',content:[{type:'input_text',text:'[本轮可用的手]\nblah'}]},
    {type:'message',role:'user',content:[{type:'input_text',text:'[上一步执行结果]\nfoo'}]}]
  const esc=P.escalatePrompt(items,'RETRY')
  assert.ok(esc.includes('USER: '+REAL_ASK),'应回到真正的最后一条用户消息')
  // 诉求本身不许被交手块/结果块顶掉；至于要不要再附尾部，看长度，见后两个用例
  assert.ok(!/USER: \[/.test(esc),'诉求位上出现了我们自己塞的块')
})

// ── 空诉求兜底（2026-08-08 线上：6 次重试 2 次因此作废）────────────────
// 签名：escalate prompt 568~572 chars（= 交手块 346 + 升级指令 190 + 空 USER:），
// 之后再没有 compiled。长 agent 循环里本轮尾部是工具结果、不是新的用户提问。
t7('短诉求照留，但要补上对话尾部（阈值不能拍脑袋）',()=>{
  const items=[{type:'additional_tools',role:'developer',tools:[]},
    {type:'message',role:'developer',content:[{type:'input_text',text:'<permissions instructions>\nsandbox'}]},
    {type:'message',role:'user',content:[{type:'input_text',text:'ll'}]},
    {type:'custom_tool_call_output',output:'src\npkg.json'}]
  const esc=P.escalatePrompt(items,'RETRY')
  assert.ok(esc.includes('USER: ll'),'ll 是真诉求，不许因为"太短"被丢掉')
  assert.ok(esc.includes('[对话尾部]'),'短诉求要补尾部')
  assert.ok(!esc.includes('permissions instructions'),'尾部不许把脚手架拖回来')
})
t7('长诉求路径逐字节不变（保住已实测 4/4 的那条路）',()=>{
  const ask='x'.repeat(300)
  const items=[{type:'additional_tools',role:'developer',tools:[]},
    {type:'message',role:'user',content:[{type:'input_text',text:ask}]},
    {type:'custom_tool_call_output',output:'noise'}]
  const esc=P.escalatePrompt(items,'RETRY')
  assert.ok(esc.includes('USER: '+ask))
  assert.ok(!esc.includes('[对话尾部]'),'诉求够长就不该再拖尾部进来')
})
t7('提取不到诉求时用对话尾部顶上，而不是发一句空的 USER:',()=>{
  const items=[{type:'additional_tools',role:'developer',tools:[]},
    {type:'message',role:'assistant',content:[{type:'output_text',text:'我看一下目录'}]},
    {type:'custom_tool_call',name:'exec',input:'tools.exec_command({cmd:["ls"]})'},
    {type:'custom_tool_call_output',output:'src\npackage.json'}]
  const esc=P.escalatePrompt(items,'RETRY')
  assert.ok(esc.includes('[对话尾部]'),'该走兜底')
  assert.ok(!/USER:\s*$/m.test(esc),'不许出现空的 USER:')
  assert.ok(esc.includes('package.json'),'尾部要带上真实的工具结果，模型才知道刚发生了什么')
  assert.ok(esc.includes('[本轮可用的手]')&&esc.includes('RETRY'))
  // 判据不能写死字符数（会随 fixture 大小变）：只要求尾部真的贡献了内容
  const empty=P.escalatePrompt([{type:'additional_tools',role:'developer',tools:[]}],'RETRY')
  assert.ok(esc.length>empty.length+50,'兜底没贡献内容，还是 568/572 那种空壳')
})
t7('兜底也要有体积上限',()=>{
  const items=[{type:'additional_tools',role:'developer',tools:[]}]
  for(let i=0;i<40;i++) items.push({type:'message',role:'assistant',
    content:[{type:'output_text',text:'x'.repeat(5000)}]})
  const esc=P.escalatePrompt(items,'RETRY')
  assert.ok(esc.length<5000,'兜底不能把长会话又拖回来: '+esc.length)
})
t7('兜底不把交手块回读成"对话内容"',()=>{
  const items=[{type:'additional_tools',role:'developer',tools:[]},
    {type:'message',role:'assistant',content:[{type:'output_text',text:'唯一的真内容'}]},
    {type:'message',role:'user',content:[{type:'input_text',text:P.handsBlock('/x')}]}]
  const d=P.tailDigest(items)
  assert.ok(d.includes('唯一的真内容'))
  assert.ok(!d.includes('[本轮可用的手]'),'交手块是我们自己塞的，回读等于自问自答')
})
t7('形状日志不含会话内容',()=>{
  const items=[{type:'message',role:'user',content:[{type:'input_text',text:'机密内容XYZ'}]}]
  const s=P.shapeOf?P.shapeOf(items):''
  if(s) assert.ok(!s.includes('机密内容'),'形状摘要泄漏了内容')
})
console.log(`\n=== 精简重试 ${p7} passed, ${f7} failed ===`)


console.log('\n-- 引用标记剥离 --')
let p8=0,f8=0
function t8(n,fn){try{fn();p8++;console.log('  ✅',n)}catch(e){f8++;console.log('  ❌',n,'\n     ',e.message)}}
const CO='', CS='', CE=''
const SAMPLE='甲=ZEBRA-7741 '+CO+'filecite'+CS+'turn0file0'+CS+'L3-L3'+CE+' 完毕'

t8('完整标记整段剥掉，正文一字不动',()=>{
  const out=P.stripCitations(SAMPLE)
  assert.ok(!/[-]/.test(out),'还有私有区字符: '+JSON.stringify(out))
  assert.ok(!out.includes('filecite')&&!out.includes('turn0file0'))
  assert.ok(out.includes('甲=ZEBRA-7741')&&out.includes('完毕'),'正文被误删: '+out)
})
t8('跨分片到达也不漏（逐片正则会切两半，各漏一截）',()=>{
  const f=P.makeCitationFilter()
  let out=''
  for(const c of ['甲=ZEBRA-7741 '+CO+'file','cite'+CS+'turn0','file0'+CS+'L3-L3'+CE+' 完','毕']) out+=f.push(c)
  assert.ok(!/[-]/.test(out)&&!out.includes('cite'),JSON.stringify(out))
  assert.ok(out.includes('甲=ZEBRA-7741')&&out.includes('完毕'))
})
t8('上游断在标记中间：残段丢弃 + flush 报 truncated',()=>{
  // 用户现场就是这个形状：status=completed 但只有起始符没有结束符
  const f=P.makeCitationFilter()
  const out=f.push('三个标记值如下：'+CO+'filecite'+CS+'turn0')
  assert.equal(out,'三个标记值如下：','残段不许漏给用户: '+JSON.stringify(out))
  const r=f.flush()
  assert.ok(r.truncated,'该报 truncated，否则这种静默截断没人知道')
  assert.ok(r.dropped>0)
})
t8('落单的分隔符/结束符也不放行',()=>{
  assert.equal(P.stripCitations('答案'+CS+'x'+CE+'y'),'答案xy')
})
t8('没有标记的正文逐字节不变（绝大多数请求走这条）',()=>{
  for(const s of ['普通回答，没有任何标记。','⟦ls¦path=/tmp⟧','']) 
    assert.strictEqual(P.stripCitations(s),s,JSON.stringify(s))
})
t8('不是标记的超长内容要放行，不能永久扣在缓冲区',()=>{
  const f=P.makeCitationFilter()
  const out=f.push(CO+'x'.repeat(400))
  assert.ok(out.length>300,'正文被永久扣住了，用户会看到回答凭空少一段')
})
t8('BPI 块混引用标记时，块要能照常编译',()=>{
  // 剥离发生在编译之前，块里若混进标记会导致参数带脏字符
  const dirty='⟦ls¦path=/tmp'+CO+'filecite'+CS+'turn0file0'+CE+'⟧'
  const r=P.compileToExec(P.stripCitations(dirty))
  assert.equal(r.blocks.length,1)
  assert.ok(!/[-]/.test(r.js),'编译产物里带了私有区字符: '+r.js)
})
t8('不引用提示存在且是纯文本',()=>{
  assert.ok(P.CITE_FREE_HINT&&P.CITE_FREE_HINT.includes('不要引用'))
  assert.ok(!/[-]/.test(P.CITE_FREE_HINT))
})
console.log(`\n=== 引用剥离 ${p8} passed, ${f8} failed ===`)
process.exit(fail+f2+f3+f4+f5+f6+f7+f8?1:0)
