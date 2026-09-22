#!/usr/bin/env node
/* 上下文窗口补丁的核查尺子(配 cursor_ctxwin_patch.js @cx-ctxwin:v2)
 * 用法: node cursor_ctxwin_check.js [state.vscdb路径]
 *       不给路径就读当前 Cursor 的库。
 * 判据(三条,缺一条都不算通过):
 *   1. maxTokens 列不再出现 {256,272,300,500} 这种小 1000 倍的值
 *   2. 百分比列回到 0~100% 区间(补前是 8146%~27706%)
 *   3. 重启后新建的会话,连发 hi 不再长 cap22 气泡
 * ⚠️ 老会话的 promptTokenBreakdown 是补前写进库的历史快照,不会自己变;
 *    只看重启之后新建的那几行。
 */
const {DatabaseSync}=require("node:sqlite");
const db=new DatabaseSync(process.argv[2]||(process.env.HOME+"/Library/Application Support/Cursor/User/globalStorage/state.vscdb"),{readOnly:true});
const hs=db.prepare("select composerId,lastUpdatedAt from composerHeaders order by lastUpdatedAt desc limit 25").all();
const sel=db.prepare("select value from cursorDiskKV where key=?");
console.log("时间(UTC)      | ctx档  | maxTok | used   | 百分比    | cap22 | 模型 / 气泡数");
console.log("-".repeat(104));
let n22=0,nses=0;
for(const r of hs){
  const row=sel.get("composerData:"+r.composerId); if(!row) continue;
  let d; try{d=JSON.parse(String(row.value))}catch(e){continue}
  const sm=((d.modelConfig||{}).selectedModels||[])[0]||{};
  const ctx=((sm.parameters||[]).find(p=>p.id==="context")||{}).value;
  const bd=d.promptTokenBreakdown||{};
  const h=d.fullConversationHeadersOnly||[];
  const c22=h.filter(x=>x.grouping&&x.grouping.capabilityType===22).length;
  n22+=c22; nses++;
  const mt=bd.maxTokens, us=bd.totalUsedTokens;
  console.log([new Date(r.lastUpdatedAt).toISOString().slice(5,16).replace("T"," "),
    String(ctx===undefined?"(无)":ctx).padEnd(6),
    String(mt===undefined?"-":mt).padEnd(6),
    String(us===undefined?"-":us).padEnd(6),
    (mt>0&&us?(us/mt*100).toFixed(1)+"%":"-").padEnd(9),
    String(c22).padEnd(5),
    ((d.modelConfig||{}).modelName||"-")+"  n="+h.length].join(" | "));
}
console.log(`\n合计 ${nses} 个会话,摘要气泡(cap22) ${n22} 个`);
db.close();
