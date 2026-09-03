// bundle_anchor_probe.js —— 只读:对当前 Cursor 的两份 bundle 数 8 个补丁锚点各命中几次。Cursor 不用退。
// 用法: ELECTRON_RUN_AS_NODE=1 /Applications/Cursor.app/Contents/MacOS/Cursor bundle_anchor_probe.js ./cursor_team_setup.js
// 判据: 每个非 multi 锚点 hits=1(或 marker=已打);hits=0 = Cursor 版本改了那处代码 → 安装器会拒绝动手(不会改坏),
// 要照 09-03 修 3.18.25 的办法(两代形状都认、仍要求恰好 1 次)补正则。
// 只读:把安装器里的 PATCHES/BUNDLES 抠出来,对当前 Cursor 的 bundle 数锚点命中
const fs=require('fs'),path=require('path');
let src=fs.readFileSync(process.argv[2],'utf8').replace(/^#!.*\n/,'');
src=src.replace(/main\(\)\.catch[\s\S]*$/,'module.exports={PATCHES,BUNDLES,RES,QP_OLD,QP_MARKER};');
src=src.replace(/^\s*if \(!fs\.existsSync\(RES\)\)[^\n]*\n/m,'');
const m={exports:{}}; new Function('module','exports','require','__dirname','__filename','process',src)(m,m.exports,require,path.dirname(process.argv[2]),process.argv[2],process);
const {PATCHES,BUNDLES,RES,QP_OLD}=m.exports;
console.log('RES=',RES);
for(const rel of BUNDLES){const p=path.join(RES,rel); if(!fs.existsSync(p)){console.log('MISSING',rel);continue}
  const s=fs.readFileSync(p,'utf8'); console.log('==',rel,(s.length/1e6).toFixed(1)+'MB');
  for(const pt of PATCHES){ pt.rx.lastIndex=0; let n=0; while(pt.rx.exec(s))n++; pt.rx.lastIndex=0;
    const done=s.includes(pt.marker); const old=pt.name==='queue-pump'&&QP_OLD.some(o=>s.includes(o));
    console.log(`  ${pt.name.padEnd(12)} hits=${n}${pt.multi?'(multi)':''} marker=${done?'已打':'-'}${old?' 旧版在':''}`)}}
