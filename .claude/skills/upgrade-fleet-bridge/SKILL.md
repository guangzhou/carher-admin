---
name: upgrade-fleet-bridge
description: 跨双轨升级桥接 — Compose 老线(S1/S2/S3)与 K8s 新线(ACK)统一升级视角。本仓负责 K8s,CarHer 主仓负责 Compose 老线;本 skill 是它们之间的导航。触发词:双轨升级 / cross-track upgrade / S1/S2/S3 还在跑 / Compose 升级 / 老线升级 / 全 fleet 升级 / GitHub→GitLab 迁移 / 模型路由跨仓.
---

# 双轨升级桥接 — carher-admin ↔ CarHer 主仓

> **本仓(`~/codes/carher-admin`)负责 K8s 新线的所有事**。但 CarHer 现在是**双轨并行**:~70 用户还在 S1/S2/S3 老线 Docker Compose 上,3 个 Pilot 在 K8s。
>
> 本 skill 解决一个问题:**"老线的事不归我管,但又必须考虑它"**。

---

## 一、🔒 第一铁律 — 判路径

```
                       目标用户/操作影响哪一侧?
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
   ❶ K8s 新线           ❷ Compose 老线       ❸ 跨双轨
   (本仓主战场)        (CarHer 主仓 deploy/) (双仓协作)
        │                   │                   │
   本仓 skill 全套      切换到 CarHer 主仓   先 ❷ 验证再 ❶
   carher-deploy        upgrade-fleet skill   见下文工作流
   hot-grayscale
   carher-upgrade-compare
```

**判定**:
```bash
# K8s 上有这个用户吗?
kc get herinstance | grep carher-<N>

# 老线 CSV 里有吗?
ssh <S1> 'awk -F, "\$1==<N>" /Data/CarHer/docker/users.csv'
```

两边都没 = 新建,默认上 K8s。两边都有 = 迁移中状态(异常,需修复)。

---

## 二、双仓职责矩阵

| 维度 | 本仓 `carher-admin` | CarHer 主仓 `~/codes/CarHer` |
|---|---|---|
| 镜像 | `her/carher-admin`(管理 Web)+ 触发 `carher-core` 部署 | **build** `carher-core` 镜像(Dockerfile.carher.v2 + `deploy/build-and-push.sh`)|
| 编排 | K8s HerInstance CRD + Operator | Compose `deploy/carher-N/compose.yaml` + `dc.sh` |
| 灰度 | Admin Web `deployer.py`(canary→early→stable)| 手工(tester → admin → spoke → 全员) |
| 自检 | Operator 健康门 + Admin Web wave 检查 | `scripts/carher-verify.sh --id=N`(11 道关)|
| 回滚 | Admin Web `/api/deploy/<id>/rollback`(SQLite 30 天) | 改 `deploy/carher-N/.env IMAGE_TAG` + `dc.sh up -d`(秒级,volume 保留)|
| Cloudflare | K8s `carher-k8s` tunnel + ConfigMap | 三服 systemd `carher-s{1,2,3}` tunnel |
| 模型路由 | LiteLLM proxy configmap | bot 端 `docker/carher-config.json` model 定义 |
| 部署 CI | GitHub Actions `build-deploy.yml` | 暂无(等 GitLab 迁移) |

---

## 三、跨双轨升级工作流(全 fleet 上新版 carher-core)

```
1. CarHer 主仓 build 新镜像
   cd ~/codes/CarHer
   ./deploy/build-and-push.sh --openclaw-tag=<v> \
     --registry=cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her

2. Mac 本地 tester 验证(carher-101..103)
   ./start-user.sh --id=101 --image=<NEW_TAG>
   ./scripts/carher-verify.sh --id=101 --wait=60
   ↓ 11 关全过 + 主人确认无回归

3. K8s Pilot 升级(本仓主战场)
   - 用 Admin Web `/api/deploy` 选 image_tag + mode=canary-only
   - 推到 carher-14 → 24h 观察期
   - 见 carher-admin 仓 `carher-deploy/SKILL.md` + `hot-grayscale/SKILL.md`

4. Compose 老线 admin + spoke 升级(CarHer 主仓负责)
   - 改 S1 carher-198(admin)/ 几个 spoke 的 .env IMAGE_TAG
   - 各跑 carher-verify.sh 11 关
   - 见 CarHer 主仓 `upgrade-fleet/SKILL.md` A 路径

5. Compose 老线全员铺开
   - wave 间隔 ≥ 12 min(ACR 公网拉镜像耗时)
   - 不动 carher-1(老杨/董事长)/ carher-3(测试机器同名),除非用户明确指令

6. K8s 全员铺开(等 K8s 用户量上来才有意义)
```

**严禁**:
- 没 tester 验证就上 Pilot
- 同时 ❶ ❷ 一起铺,出问题无法二分定位

---

## 四、模型升级跨仓两步走

加新模型 / 升级模型时:

```
1. 本仓:加 LiteLLM 路由
   - 改 k8s/litellm-proxy.yaml(或对应 ConfigMap)的 model_list
   - kubectl apply 触发 reload
   - 见本仓 add-litellm-model/SKILL.md
   
2. CarHer 主仓:加 model 定义
   - 改 docker/carher-config.json(image-level)+ docker/shared-config.json5(server-shared)
   - 关键铁律:agents.defaults.contextTokens 必须等于所有模型 contextWindow
   - anthropic provider 必须有 apiKey(否则 models.json 静默丢弃)
   - 见 CarHer 主仓 upgrade-model/SKILL.md

只改一边 = 401/404。两边都改 + 走 upgrade-fleet 触发部署。
```

---

## 五、Cloudflare DNS 切换边界

| 用户在哪 | DNS 该指向 | 谁改 |
|---|---|---|
| Compose 老线 | `carher-s{1,2,3}` tunnel | CarHer 主仓 / S1/S2/S3 系统管理员手工 |
| K8s 新线 | `carher-k8s` tunnel | 本仓 Admin Web `cloudflare_ops.py` 自动 |
| 跨服迁移瞬间 | 切换瞬间 30-60s 掉线 | Admin Web `wait_for_service` 轮询保护 |

**严禁**:同一 tunnel 两条 connector(50% 请求 404,已知坑)、两侧 tunnel 共用。

---

## 六、GitHub → GitLab 迁移影响

```
当前 CI 入口  : github.com/guangzhou/carher-admin (Actions build-deploy.yml)
迁移后       : GitLab(等 IT 推进,影响以下)
              - AppSet repoURL(deploy/appset.yaml 占位符)
              - GitHub Secret → GitLab CI/CD Variables
              - CarHer 主仓首次接入 CI(目前无自动 build)
```

迁移完成才真激活 ArgoCD ApplicationSet,目前 K8s 实例由 Admin Web 直接 patch 创建。

---

## 七、关键 CarHer 主仓 skill 导航

需要操作老线 / 跨仓时,直接读这些(在 CarHer 主仓 `.cursor/skills/` 下):

| 主题 | 路径 |
|---|---|
| 双轨升级唯一入口 | `~/codes/CarHer/.cursor/skills/upgrade-fleet/SKILL.md` |
| 老线运维全景 | `~/codes/CarHer/.cursor/skills/carher-ops/SKILL.md` |
| 双镜像构建 | `~/codes/CarHer/.cursor/skills/carher-image-build-push/SKILL.md` |
| Compose fleet admin | `~/codes/CarHer/.cursor/skills/docker-fleet/SKILL.md` |
| 模型元数据 + 三层 config | `~/codes/CarHer/.cursor/skills/upgrade-model/SKILL.md` |
| 镜像 13 个 runtime patches | `~/codes/CarHer/scripts/carher-patches/` + 主仓 `CLAUDE.md` |
| 11 关自检 | `~/codes/CarHer/scripts/carher-verify.sh` |

---

## 八、Footguns

- **以为 K8s 已经是全部** — 错。70+ 用户还在 Compose,主线在那。K8s 是 Pilot。
- **同时触发 ❶ ❷ 升级** — 出问题无法定位是 K8s 路径 bug 还是镜像 bug。先 ❷ tester+K8s pilot,再铺老线 + K8s 全员。
- **改 LiteLLM 配置不通知 CarHer 主仓** — bot 端 model 定义没改,401/404。两个仓必须同步改。
- **本仓改完就 push** — 注意 SQLite 历史在 K8s NAS PVC,回滚走 Admin Web 不是 git revert。
- **以为 ApplicationSet 在 work** — `deploy/appset.yaml` 的 `repoURL` 还是占位符,等 GitLab 迁移才激活。当前部署 = Admin Web 直接 patch。
- **跳过 CarHer 主仓的 carher-verify.sh 11 关** — 那 11 关里 5 个 runtime patch marker 是判断镜像健康的唯一方式;K8s Pod exec 进去也要跑这个。
