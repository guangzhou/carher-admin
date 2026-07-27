// test-plane-classify.js — mcp-connector-cli.js 平面分类回归测试
//
// 为什么需要:403 单看会骗人。403+HTML 是 Cloudflare(信息量为零),
// 403+JSON "X is required" 是功能门(路由存在)。把这两者混淆会导致
// "账号没权限"的错误结论 —— 本 session 真实踩过。
// 每条 case 都是实测过的响应形态。
// 跑: node testkit/test-plane-classify.js
const fs=require("fs");
const src=fs.readFileSync(require("path").join(__dirname,"..","mcp-connector-cli.js"),"utf8");
const m=src.match(/function classify[\s\S]*?\n\}/)[0];
const classify=eval("("+m.replace(/^function classify/,"function")+")");
const CASES=[
 ["Cloudflare(漏路由头)",403,false,null,"CLOUDFLARE"],
 ["功能门 devmode",403,true,{detail:"Developer mode is required"},"FEATURE_GATE"],
 ["路由不存在",404,true,{detail:"Not Found"},"NO_ROUTE"],
 ["路径段当ID",404,true,{detail:"Connector not found"},"ROUTE_OK_BAD_ID"],
 ["method不对",405,true,{detail:"Method Not Allowed"},"WRONG_METHOD"],
 ["schema错(query)",422,true,{detail:[{loc:["query","feature"]}]},"SCHEMA"],
 ["成功",200,true,{tunnels:[]},"OK"],
];
let bad=0;
for(const [n,s,j,b,exp] of CASES){
 const got=classify(s,j,b);
 const ok=got===exp; if(!ok)bad++;
 console.log((ok?"PASS":"FAIL")+"  "+n+" → "+got+(ok?"":" 期望 "+exp));
}
console.log(bad?("\n"+bad+" 条失败"):"\n7/7 通过");
process.exit(bad?1:0);
