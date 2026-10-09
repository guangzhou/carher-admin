# vendor/ — 来自 kiro-register-en 的第三方脚本

来源：https://github.com/nopperabbo/kiro-register-en
（GALIAIS/k_i_r_o-register 的英文 fork）

拉取日期：2026-10-08

| 文件 | 角色 |
|------|------|
| `kiro_login.py` | 手动登录（Google/GitHub/BuilderId/IAM），async Playwright |
| `kiro_register.py` | 批量自动注册（状态机 + 邮件 OTP + hCaptcha） |
| `kiro_subscribe.py` | Pro 订阅管理（Stripe checkout） |
| `main_gui.py` | Tkinter GUI 入口（原名 main.py） |

⚠️ 这些是参考实现，不是直接跑的：
- 依赖 `curl_cffi`、`playwright-stealth`（venv 已装）
- `kiro_login.py` 是 async 的，直接 `asyncio.run()` 调
- `kiro_register.py` 有完整的 profile.aws 页面导航逻辑（SPA + TES warmup）

我们自己的脚本在上一级目录，复用了这些的思路但做了适配：
- `kiro_oneshot.py` 的 `drive_login()` 借鉴了 `kiro_register.py` 的状态机
- `aws_mfa_rotate.py` 的 warmup/JS click 借鉴了 `kiro_register.py` 的反 TES 策略
