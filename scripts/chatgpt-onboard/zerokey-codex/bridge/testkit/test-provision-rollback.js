// test-provision-rollback.js -- provision 的回滚控制流
//
// 为什么需要:register 成功之后的任一步失败,如果不 del(),账号上就留下一个
// 没有 link 的孤儿 connector —— 模型看不到它,而下次 provision 又会再建一个,
// 越堆越多。API 没有 list-by-account,堆积后很难清理。
// 静态检查控制流,不打真 API。
//
// 跑: node testkit/test-provision-rollback.js
// 验证 provision 的回滚控制流:register 成功后任一步失败都必须 del()
const fs=require('fs');
const src=fs.readFileSync(require('path').join(__dirname,'..','mcp-connector-cli.js'),'utf8');
const body=src.match(/async function provision[\s\S]*?\n\}/)[0];
const checks=[
 ["actions 为空时调用 rollback", /if \(!list\.length\) return rollback\(/],
 ["link 失败时调用 rollback",     /if \(!linkId\) return rollback\(/],
 ["rollback 里真的 del",          /const rollback[\s\S]*?await del\(h, id\)/],
 ["rollback 失败会提示手删",       /回滚失败,请手动删除/],
];
let bad=0;
for(const [n,re] of checks){ const ok=re.test(body); if(!ok)bad++; console.log((ok?"PASS  ":"FAIL  ")+n); }
console.log(bad?("\n"+bad+" 失败"):"\n4/4 通过");
process.exit(bad?1:0);
