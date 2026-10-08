# shellcheck shell=bash
# 198 OWUI 运维脚本的公共函数。被 source,不单独跑。
#
# 连接方式照仓里 198 脚本惯例:sshpass 直连内网 IP,不走 jms(jms 吞 sudo 密码、PTY 丢块)。
# 本机需要:sshpass、env P198(198 cltx 密码)。
#
# 所有远端命令都 base64 打包后再下发,避免多层引号塌掉:
#   r198 '<bash 脚本>'                     在 198 上以 root 跑一段 bash
#   pypod <ns> <deploy> <py文件> [json参数]  在 pod 里跑本地 python 文件,参数经 ARGS 变量注入
#
# 为什么不用 heredoc / kubectl exec -i:见 memory kubectl_exec_i_eats_heredoc_stdin、
# feedback_jms_ssh_multiline_python_needs_heredoc_not_dash_c。

: "${P198:?env P198 required (198 cltx 密码)}"
M198=${M198:-10.68.13.198}

_b64() { base64 | tr -d '\n'; }

r198() {
  local b
  b=$(printf '%s\n' "$1" | _b64)
  sshpass -p "$P198" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 "cltx@$M198" \
    "printf '%s\n' '$P198' | sudo -S -p '' bash -c \"\$(echo $b | base64 -d)\""
}

# pypod NS DEPLOY FILE [JSON]
# python 在 pod 的 /app/backend 下跑(OWUI 的 open_webui 包要从这里 import)。
pypod() {
  local ns=$1 dep=$2 file=$3 args=${4:-'{}'} src
  src=$( { printf 'import json, base64\nARGS = json.loads(base64.b64decode("%s").decode())\n' \
            "$(printf '%s' "$args" | _b64)"; cat "$file"; } | _b64)
  r198 "kubectl exec -n $ns deploy/$dep -- sh -c 'cd /app/backend 2>/dev/null; echo $src | base64 -d | python3 - 2>&1' \
        | grep -vE '^(INFO|DEBUG|WARNI) |^WARNING: |^\$|sitecustomize\\]|pypatch '"
}
