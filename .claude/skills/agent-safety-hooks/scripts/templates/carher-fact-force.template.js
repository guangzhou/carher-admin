#!/usr/bin/env node
/**
 * carher-fact-force.template.js
 *
 * Carher-admin 版的 Fact-Forcing Gate hook。
 * 第一次操作目标 → 拒绝 + 给 LLM 一份取证清单（强制 Read/Grep/kubectl describe）
 * 第二次同样的目标 → 放行
 *
 * 来源：affaan-m/everything-claude-code 的 scripts/hooks/gateguard-fact-force.js (416 行)
 * 改造：取证清单针对 carher 运维场景（k8s yaml / litellm callback / 破坏性 kubectl）
 *
 * 安装：
 *   cp this-file .cursor/hooks/carher-fact-force.js
 *   找 // CARHER:CUSTOMIZE 标记按你的环境改
 *
 * 干跑测试：
 *   echo '{"tool_name":"Edit","tool_input":{"file_path":"/path/to/k8s/litellm-proxy.yaml"}}' \
 *     | node .cursor/hooks/carher-fact-force.js
 */

'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

// ─────────────────────────────────────────────────────────────────────────────
// State management (per-session, atomic writes, bounded)
// 不要为了简化删掉这些工程细节
// ─────────────────────────────────────────────────────────────────────────────

const STATE_DIR =
  process.env.CARHER_FACTFORCE_STATE_DIR ||
  path.join(process.env.HOME || process.env.USERPROFILE || '/tmp', '.carher-fact-force');

let activeStateFile = null;

const SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 分钟
const READ_HEARTBEAT_MS = 60 * 1000;
const MAX_CHECKED_ENTRIES = 500;
const MAX_SESSION_KEYS = 50;
const ROUTINE_BASH_SESSION_KEY = '__bash_session__';

// CARHER:CUSTOMIZE destructive-regex
// 列出在 carher 集群里跑会有破坏性影响的命令
const DESTRUCTIVE_BASH = new RegExp(
  '\\b(' +
    [
      'rm\\s+-rf',
      'git\\s+reset\\s+--hard',
      'git\\s+checkout\\s+--',
      'git\\s+clean\\s+-f',
      'git\\s+push\\s+--force',
      'dd\\s+if=',
      'drop\\s+table',
      'delete\\s+from',
      'truncate',
      // ▼ carher / k8s 特化
      'kubectl\\s+(delete|drain|cordon)',
      'kubectl\\s+rollout\\s+restart',
      'kubectl\\s+scale\\s+[^\\n]*--replicas=0',
      'kubectl\\s+exec\\s+[^\\n]*\\brm\\b',
      'helm\\s+(uninstall|delete)',
    ].join('|') +
    ')\\b',
  'i'
);

function sanitizeSessionKey(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const sanitized = raw.replace(/[^a-zA-Z0-9_-]/g, '_');
  if (sanitized && sanitized.length <= 64) return sanitized;
  return hashSessionKey('sid', raw);
}

function hashSessionKey(prefix, value) {
  return `${prefix}-${crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 24)}`;
}

function resolveSessionKey(data) {
  const directCandidates = [
    data && data.session_id,
    data && data.sessionId,
    data && data.session && data.session.id,
    process.env.CLAUDE_SESSION_ID,
    process.env.CURSOR_SESSION_ID,
    process.env.ECC_SESSION_ID,
  ];
  for (const candidate of directCandidates) {
    const sanitized = sanitizeSessionKey(candidate);
    if (sanitized) return sanitized;
  }
  const transcriptPath =
    (data && (data.transcript_path || data.transcriptPath)) ||
    process.env.CLAUDE_TRANSCRIPT_PATH;
  if (transcriptPath && String(transcriptPath).trim()) {
    return hashSessionKey('tx', path.resolve(String(transcriptPath).trim()));
  }
  const projectFingerprint =
    process.env.CLAUDE_PROJECT_DIR || process.env.CURSOR_PROJECT_DIR || process.cwd();
  return hashSessionKey('proj', path.resolve(projectFingerprint));
}

function getStateFile(data) {
  if (!activeStateFile) {
    const sessionKey = resolveSessionKey(data);
    activeStateFile = path.join(STATE_DIR, `state-${sessionKey}.json`);
  }
  return activeStateFile;
}

function loadState() {
  const stateFile = getStateFile();
  try {
    if (fs.existsSync(stateFile)) {
      const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
      const lastActive = state.last_active || 0;
      if (Date.now() - lastActive > SESSION_TIMEOUT_MS) {
        try {
          fs.unlinkSync(stateFile);
        } catch (_) {}
        return { checked: [], last_active: Date.now() };
      }
      return state;
    }
  } catch (_) {}
  return { checked: [], last_active: Date.now() };
}

function pruneCheckedEntries(checked) {
  if (checked.length <= MAX_CHECKED_ENTRIES) return checked;
  const preserved = checked.includes(ROUTINE_BASH_SESSION_KEY) ? [ROUTINE_BASH_SESSION_KEY] : [];
  const sessionKeys = checked.filter(k => k.startsWith('__') && k !== ROUTINE_BASH_SESSION_KEY);
  const fileKeys = checked.filter(k => !k.startsWith('__'));
  const remainingSessionSlots = Math.max(MAX_SESSION_KEYS - preserved.length, 0);
  const cappedSession = sessionKeys.slice(-remainingSessionSlots);
  const remainingFileSlots = Math.max(MAX_CHECKED_ENTRIES - preserved.length - cappedSession.length, 0);
  const cappedFiles = fileKeys.slice(-remainingFileSlots);
  return [...preserved, ...cappedSession, ...cappedFiles];
}

function saveState(state) {
  const stateFile = getStateFile();
  let tmpFile = null;
  try {
    state.last_active = Date.now();
    state.checked = pruneCheckedEntries(state.checked);
    fs.mkdirSync(STATE_DIR, { recursive: true });
    tmpFile = stateFile + '.tmp.' + process.pid;
    fs.writeFileSync(tmpFile, JSON.stringify(state, null, 2), 'utf8');
    try {
      fs.renameSync(tmpFile, stateFile);
    } catch (error) {
      if (error && (error.code === 'EEXIST' || error.code === 'EPERM')) {
        try {
          fs.unlinkSync(stateFile);
        } catch (_) {}
        fs.renameSync(tmpFile, stateFile);
      } else {
        throw error;
      }
    }
  } catch (_) {
    if (tmpFile) {
      try {
        fs.unlinkSync(tmpFile);
      } catch (_) {}
    }
  }
}

function markChecked(key) {
  const state = loadState();
  if (!state.checked.includes(key)) {
    state.checked.push(key);
    saveState(state);
  }
}

function isChecked(key) {
  const state = loadState();
  const found = state.checked.includes(key);
  if (found && Date.now() - (state.last_active || 0) > READ_HEARTBEAT_MS) {
    saveState(state);
  }
  return found;
}

(function pruneStaleFiles() {
  try {
    const files = fs.readdirSync(STATE_DIR);
    const now = Date.now();
    for (const f of files) {
      if (!f.startsWith('state-') || !f.endsWith('.json')) continue;
      const fp = path.join(STATE_DIR, f);
      try {
        const stat = fs.statSync(fp);
        if (now - stat.mtimeMs > SESSION_TIMEOUT_MS * 2) fs.unlinkSync(fp);
      } catch (_) {}
    }
  } catch (_) {}
})();

// ─────────────────────────────────────────────────────────────────────────────
// Path sanitize (anti prompt-injection via filename)
// 防 unicode bidi override / 控制字符走私
// ─────────────────────────────────────────────────────────────────────────────

function sanitizePath(filePath) {
  let sanitized = '';
  for (const char of String(filePath || '')) {
    const code = char.codePointAt(0);
    const isAsciiControl = code <= 0x1f || code === 0x7f;
    const isBidiOverride =
      (code >= 0x200e && code <= 0x200f) ||
      (code >= 0x202a && code <= 0x202e) ||
      (code >= 0x2066 && code <= 0x2069);
    sanitized += isAsciiControl || isBidiOverride ? ' ' : char;
  }
  return sanitized.trim().slice(0, 500);
}

function normalizeForMatch(value) {
  return String(value || '').replace(/\\/g, '/').toLowerCase();
}

// CARHER:CUSTOMIZE allowlist
// 永远放行的路径（cursor / claude 自己的设置，不拦否则卡死）
function isAllowlistedPath(filePath) {
  const normalized = normalizeForMatch(filePath);
  if (/(^|\/)\.claude\/settings(?:\.[^/]+)?\.json$/.test(normalized)) return true;
  if (/(^|\/)\.cursor\/(state|cache)\//.test(normalized)) return true;
  return false;
}

// CARHER:CUSTOMIZE classify
// 路径分类，决定用哪一套取证清单
function classifyFilePath(filePath) {
  const norm = normalizeForMatch(filePath);
  if (/\/k8s\/litellm-proxy\.ya?ml$/.test(norm)) return 'kube-config-litellm';
  if (/\/k8s\/her-instance.*\.ya?ml$/.test(norm)) return 'kube-config-her';
  if (/\/k8s\/.+\.ya?ml$/.test(norm)) return 'kube-config-generic';
  if (/\/k8s\/litellm-callbacks\/.+\.py$/.test(norm)) return 'litellm-callback';
  if (/\/cloudflare\/tunnels\/.+\.json$/.test(norm)) return 'cf-tunnel';
  return 'normal';
}

// ─────────────────────────────────────────────────────────────────────────────
// Bash whitelisting
// 只读 git introspection 直接放行，且禁止 shell 元字符防注入
// ─────────────────────────────────────────────────────────────────────────────

function isReadOnlyGitIntrospection(command) {
  const trimmed = String(command || '').trim();
  if (!trimmed || /[\r\n;&|><`$()]/.test(trimmed)) return false;
  const tokens = trimmed.split(/\s+/);
  if (tokens[0] !== 'git' || tokens.length < 2) return false;
  const subcommand = tokens[1].toLowerCase();
  const args = tokens.slice(2);
  if (subcommand === 'status') {
    return args.every(arg => ['--porcelain', '--short', '--branch'].includes(arg));
  }
  if (subcommand === 'diff') {
    return args.length <= 1 && args.every(arg => ['--name-only', '--name-status'].includes(arg));
  }
  if (subcommand === 'log') {
    return args.every(arg => arg === '--oneline' || /^--max-count=\d+$/.test(arg));
  }
  if (subcommand === 'branch') return args.length === 1 && args[0] === '--show-current';
  if (subcommand === 'rev-parse') {
    return args.length === 2 && args[0] === '--abbrev-ref' && /^head$/i.test(args[1]);
  }
  return false;
}

// 同样可以白名单只读的 kubectl introspection（按需启用）
function isReadOnlyKubectlIntrospection(command) {
  const trimmed = String(command || '').trim();
  if (!trimmed || /[\r\n;&|><`$()]/.test(trimmed)) return false;
  const tokens = trimmed.split(/\s+/);
  if (tokens[0] !== 'kubectl' || tokens.length < 2) return false;
  const sub = tokens[1].toLowerCase();
  return ['get', 'describe', 'logs', 'top', 'explain', 'version', 'config', 'cluster-info'].includes(sub);
}

// ─────────────────────────────────────────────────────────────────────────────
// Gate messages — 取证清单
// CARHER:CUSTOMIZE gate-msg
// 这是本 hook 最关键的部分，按 carher 运维场景定制
// 详见 references/protected-files-catalog.md
// ─────────────────────────────────────────────────────────────────────────────

function editGateMsgKubeConfig(filePath, kind) {
  const safe = sanitizePath(filePath);
  const resourceHint =
    kind === 'kube-config-litellm'
      ? '(litellm proxy: deployment/service/configmap/probe)'
      : kind === 'kube-config-her'
        ? '(her instance: deployment + PVC + configmap)'
        : '(generic k8s resource)';
  return [
    '[Carher Fact-Forcing Gate — kube-config]',
    '',
    `Before editing ${safe} ${resourceHint}, present these facts:`,
    '',
    '1. Run kubectl describe for the resource this file targets and paste the current spec',
    '   (e.g., `kubectl -n <ns> describe deploy/<name>` or `kubectl get cm <name> -o yaml`)',
    '2. Identify which her instances / pods / deployments this change affects (count + names)',
    '3. Show the exact rollback command (e.g., `kubectl rollout undo deployment/<name>` or revert apply)',
    '4. State the blast radius: how many pods will restart? Estimated rollout window?',
    '   Will any in-flight WebSocket / streaming session be interrupted?',
    "5. Quote the user's current instruction verbatim (one paragraph)",
    '',
    'Present the facts, then retry the same Edit/Write operation.',
  ].join('\n');
}

function editGateMsgLitellmCallback(filePath) {
  const safe = sanitizePath(filePath);
  return [
    '[Carher Fact-Forcing Gate — litellm-callback]',
    '',
    `Before editing ${safe} (LiteLLM callback module currently serving production traffic), present:`,
    '',
    '1. List which callback hooks this file registers (async_pre_call_hook /',
    '   async_post_call_streaming_iterator_hook / log events / module-level monkey-patches)',
    '2. Show the current ConfigMap contents: `kubectl -n carher get cm litellm-callbacks -o yaml`',
    '3. Confirm: does this change require a pod restart, or is it hot-reloaded by the watcher?',
    '   Reference: skills/k8s-configmap-mount-debug',
    '4. Identify the canary/grayscale plan: which pods or env-var gate sees this change first?',
    '   (If none — say so and propose one before merging)',
    '5. Confirm a regression test exists in k8s/litellm-callbacks/tests/ for this hook',
    "6. Quote the user's current instruction verbatim",
    '',
    'Present the facts, then retry.',
  ].join('\n');
}

function editGateMsgGeneric(filePath) {
  const safe = sanitizePath(filePath);
  return [
    '[Carher Fact-Forcing Gate]',
    '',
    `Before editing ${safe}, present these facts:`,
    '',
    '1. List ALL files that import / require / reference this file (use Grep)',
    '2. List the public functions / classes / exports affected by this change',
    '3. If this file reads/writes data files, show field names, structure, and date format',
    '   (use redacted or synthetic values, not raw production data)',
    "4. Quote the user's current instruction verbatim",
    '',
    'Present the facts, then retry.',
  ].join('\n');
}

function writeGateMsg(filePath) {
  const safe = sanitizePath(filePath);
  return [
    '[Carher Fact-Forcing Gate]',
    '',
    `Before creating ${safe}, present these facts:`,
    '',
    '1. Name the file(s) and line(s) that will call / import this new file',
    '2. Confirm no existing file serves the same purpose (use Glob)',
    '3. Identify which skill / rule / runbook this belongs in (if any)',
    "4. Quote the user's current instruction verbatim",
    '',
    'Present the facts, then retry.',
  ].join('\n');
}

function destructiveBashMsg(command) {
  const safeCmd = String(command || '').slice(0, 200);
  return [
    '[Carher Fact-Forcing Gate — destructive command]',
    '',
    `Destructive command detected: ${safeCmd}`,
    'Before running, present:',
    '',
    '1. List ALL resources this command will modify or delete',
    '   (pods / PVCs / deployments / configmaps / instances by name, not just count)',
    '2. State expected user impact:',
    '   - WebSocket disconnect? Message loss?',
    '   - Affected user count?',
    '   - Time window before service resumes?',
    '3. Write the EXACT rollback command',
    '4. Confirm timing: is this a low-traffic window? Does it match canary/grayscale practice?',
    "5. Quote the user's current instruction verbatim",
    '',
    'Present the facts, then retry.',
  ].join('\n');
}

function routineBashMsg() {
  return [
    '[Carher Fact-Forcing Gate — first bash this session]',
    '',
    'Before the first Bash command this session, present these facts:',
    '',
    '1. The current user request in one sentence',
    '2. What this specific command verifies, produces, or changes',
    '',
    'Present the facts, then retry.',
  ].join('\n');
}

// ─────────────────────────────────────────────────────────────────────────────
// Deny helper
// ─────────────────────────────────────────────────────────────────────────────

function denyResult(reason) {
  return {
    stdout: JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        permissionDecision: 'deny',
        permissionDecisionReason: reason,
      },
    }),
    exitCode: 0,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Core logic (exported for tests / dispatcher integration)
// ─────────────────────────────────────────────────────────────────────────────

function pickEditGateMsg(filePath) {
  const kind = classifyFilePath(filePath);
  if (kind === 'kube-config-litellm' || kind === 'kube-config-her' || kind === 'kube-config-generic') {
    return editGateMsgKubeConfig(filePath, kind);
  }
  if (kind === 'litellm-callback') return editGateMsgLitellmCallback(filePath);
  return editGateMsgGeneric(filePath);
}

function run(rawInput) {
  let data;
  try {
    data = typeof rawInput === 'string' ? JSON.parse(rawInput) : rawInput;
  } catch (_) {
    return rawInput;
  }
  activeStateFile = null;
  getStateFile(data);

  const rawToolName = data.tool_name || '';
  const toolInput = data.tool_input || {};
  const TOOL_MAP = { edit: 'Edit', write: 'Write', multiedit: 'MultiEdit', bash: 'Bash' };
  const toolName = TOOL_MAP[rawToolName.toLowerCase()] || rawToolName;

  if (toolName === 'Edit' || toolName === 'Write') {
    const filePath = toolInput.file_path || '';
    if (!filePath || isAllowlistedPath(filePath)) return rawInput;
    if (!isChecked(filePath)) {
      markChecked(filePath);
      return denyResult(toolName === 'Edit' ? pickEditGateMsg(filePath) : writeGateMsg(filePath));
    }
    return rawInput;
  }

  if (toolName === 'MultiEdit') {
    const edits = toolInput.edits || [];
    for (const edit of edits) {
      const filePath = edit.file_path || '';
      if (filePath && !isAllowlistedPath(filePath) && !isChecked(filePath)) {
        markChecked(filePath);
        return denyResult(pickEditGateMsg(filePath));
      }
    }
    return rawInput;
  }

  if (toolName === 'Bash') {
    const command = toolInput.command || '';
    if (isReadOnlyGitIntrospection(command)) return rawInput;
    if (isReadOnlyKubectlIntrospection(command)) return rawInput;
    if (DESTRUCTIVE_BASH.test(command)) {
      const key = '__destructive__' + crypto.createHash('sha256').update(command).digest('hex').slice(0, 16);
      if (!isChecked(key)) {
        markChecked(key);
        return denyResult(destructiveBashMsg(command));
      }
      return rawInput;
    }
    if (!isChecked(ROUTINE_BASH_SESSION_KEY)) {
      markChecked(ROUTINE_BASH_SESSION_KEY);
      return denyResult(routineBashMsg());
    }
    return rawInput;
  }

  return rawInput;
}

module.exports = { run };

// ─────────────────────────────────────────────────────────────────────────────
// Stdin entry point — fail-closed on truncation
// ─────────────────────────────────────────────────────────────────────────────

if (require.main === module) {
  let raw = '';
  const MAX_STDIN = 1024 * 1024;
  let truncated = false;

  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => {
    if (raw.length < MAX_STDIN) {
      const remaining = MAX_STDIN - raw.length;
      raw += chunk.substring(0, remaining);
      if (chunk.length > remaining) truncated = true;
    } else {
      truncated = true;
    }
  });

  process.stdin.on('end', () => {
    if (truncated) {
      process.stderr.write(
        'BLOCKED: Hook input exceeded ' +
          MAX_STDIN +
          ' bytes. Refusing to bypass carher-fact-force on truncated payload.\n'
      );
      process.exit(2);
    }
    const result = run(raw);
    if (typeof result === 'string') {
      process.stdout.write(result);
      process.exit(0);
    }
    if (result && result.stdout) process.stdout.write(result.stdout);
    process.exit(typeof result.exitCode === 'number' ? result.exitCode : 0);
  });
}
