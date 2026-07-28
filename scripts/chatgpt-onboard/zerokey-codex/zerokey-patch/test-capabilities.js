#!/usr/bin/env node
/**
 * test-capabilities.js -- zerokey pod 能力目录回归测试(离线,不联网)
 *
 * 钉住三件事:
 *   1. 19 个模型与 2026-07-27 实测的 /backend-api/models 一致,且不含已退役 slug
 *   2. harvest 白名单**只有** container.exec + python
 *      (模型自述 ~34 个命名空间,但抓包证明只有 5 个会触发,其中只有这两个
 *       带可 harvest 的 content_type:"code" 体)
 *   3. web-tools.js 用共享常量而非自己硬编码一份 —— 防止两处漂移
 *
 * 跑: node test-capabilities.js   (从 zerokey-patch/ 或任意目录)
 */

'use strict';

const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const c = require(path.join(HERE, 'config', 'constants'));

let bad = 0;
function chk(name, cond, extra) {
  if (!cond) bad++;
  console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (cond || !extra ? '' : '  -> ' + extra));
}

// ── 1. 模型目录 ───────────────────────────────────────────────
// 实测 GET https://chatgpt.com/backend-api/models,2026-07-27
const EXPECTED = [
  'gpt-5-6-pro', 'gpt-5-5-pro', 'gpt-5-5-thinking', 'gpt-5-6-thinking',
  'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
  'gpt-5.5', 'gpt-5.5-cca', 'gpt-5-4-t-mini',
  'o3', 'o3-pro',
  'gpt-5-5', 'gpt-5-5-instant', 'gpt-5-5-mini', 'gpt-5-3', 'gpt-5-3-instant',
  'gpt-5-3-mini', 'research',
];
// 已从上游 /backend-api/models 消失。上游仍会对它们回 200,所以只靠"能不能调"
// 发现不了 —— 必须靠这份清单挡住。
const RETIRED = ['gpt-5-4-pro', 'gpt-5-4-thinking', 'gpt-5-2', 'gpt-5-1',
                 'gpt-5', 'gpt-5-mini', 'gpt-4-5', 'agent-mode'];

const got = c.WEB_MODEL_SLUGS;
chk(`模型数 = ${EXPECTED.length}`, got.length === EXPECTED.length, `实际 ${got.length}`);
const missing = EXPECTED.filter((s) => !got.includes(s));
chk('19 个实测模型全在', missing.length === 0, missing.join(','));
const stale = RETIRED.filter((s) => got.includes(s));
chk('无已退役 slug', stale.length === 0, stale.join(','));
chk('MODELS 与 SLUGS 一致', Object.keys(c.MODELS).length === got.length);

// 最大上下文必须能查到 —— 客户端按上下文选模型时依赖它
chk('gpt-5-6-pro 上下文 410000', c.MODELS['gpt-5-6-pro'].context_window === 410000,
    String(c.MODELS['gpt-5-6-pro'].context_window));
chk('gpt-5.6-sol 上下文 262144', c.MODELS['gpt-5.6-sol'].context_window === 262144);
const noCtx = got.filter((s) => !c.WEB_MODEL_CONTEXT[s]);
chk('每个 slug 都有上下文', noCtx.length === 0, noCtx.join(','));

// /v1/models 的 OpenAI 形状
const m = c.MODELS['gpt-5-6-pro'];
chk('模型对象形状正确', m.id === 'gpt-5-6-pro' && m.object === 'model' && !!m.owned_by);

// -wm 变体绝不能进目录:它触发 conduit stream_handoff,直连时只回 973 字节
// 的 resume token、零正文。实测 gpt-5.6-sol=17004 字节有正文 vs -wm=973 无正文。
const wm = got.filter((s) => s.endsWith('-wm'));
chk('不含 -wm 变体(会 stream_handoff 空返)', wm.length === 0, wm.join(','));

// ── 2. harvest 白名单 ────────────────────────────────────────
chk('harvest 白名单 = [container.exec, python]',
    JSON.stringify(c.HARVESTABLE_RECIPIENTS) === JSON.stringify(['container.exec', 'python']),
    JSON.stringify(c.HARVESTABLE_RECIPIENTS));
// 这三个实测从未触发(连"给我下载链接"都只回 sandbox: 文本链接),
// 加进来就是死代码
for (const r of ['container.download', 'container.feed_chars', 'container.open_image']) {
  chk(`不含从未触发的 ${r}`, !c.HARVESTABLE_RECIPIENTS.includes(r));
}
// 这两个是 MCP connector 平面,OpenAI 服务端自己执行,没有东西可 harvest
for (const r of ['api_tool.call_tool', 'web.run']) {
  chk(`不含无需 harvest 的 ${r}`, !c.HARVESTABLE_RECIPIENTS.includes(r));
}

// ── 3. 无重复定义(防漂移)────────────────────────────────────
const wt = fs.readFileSync(path.join(HERE, 'routes', 'web-tools.js'), 'utf8');
chk('web-tools.js 引用共享常量', wt.includes('HARVESTABLE_RECIPIENTS'));
chk('web-tools.js 不再硬编码 recipient 对',
    !/rec !== 'container\.exec' && rec !== 'python'/.test(wt));
// 旧注释断言"web 端永不发原生 tool_call",本 session 已证伪
chk('已删除被证伪的 never-emits 断言', !wt.includes('never emits native tool_calls'));

console.log(bad ? `\n${bad} 条失败` : '\n全部通过');
process.exit(bad ? 1 : 0);
