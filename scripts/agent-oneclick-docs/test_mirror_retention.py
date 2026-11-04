"""镜像「搬挂载盘 + 只留最近 2 个版本」的离线台架(2026-11-04)。

按 198 上 11-04 的**真实布局**造假目录(版本名、稳定软链的指向都照抄现场),
所以这里断言「删掉哪些 / 留下哪些」= 第一次在 198 上真跑会发生的事。
为什么要这么较真:删错一个 = 同事下载链接 404,或者桌面 App 发消息不回 —— 而且是**静默**的。

需要 GNU 工具(find -printf / sort -V / du -sb / mountpoint)。macOS 上跳过,在 198(Linux)上跑:
  python3 -m pytest scripts/agent-oneclick-docs/test_mirror_retention.py -q
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LIB = HERE / "mirror-retention.sh"


def _gnu():
    try:
        return "GNU" in subprocess.run(["find", "--version"], capture_output=True, text=True).stdout
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _gnu(), reason="需要 GNU findutils/coreutils(在 198 上跑)")


def mk(p, size=1024):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def link(at, target):
    at.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, at)


def build_198_layout(root: Path):
    """11-04 198 现场:/var/www/ccdl 下的版本目录 + 稳定软链指向(readlink 原样)。"""
    apps = root / "apps"
    mk(apps / "cursor/3.28.7/Cursor-mac.dmg")
    mk(apps / "codex/26.1027.6604.0/Codex-windows-x64.msix")
    mk(apps / "codex/mac-26.1029.61003/Codex-mac.dmg")
    for v in ("3.6556.2", "4.16336.0", "4.29.0", "4.4212.0"):
        mk(apps / f"claude/{v}/Claude-linux-amd64.deb")
    link(apps / "Cursor-mac.dmg", "cursor/3.28.7/Cursor-mac.dmg")
    link(apps / "Codex-windows-x64.msix", "codex/26.1027.6604.0/Codex-windows-x64.msix")
    link(apps / "Codex-mac.dmg", "codex/mac-26.1029.61003/Codex-mac.dmg")
    # 现场:Claude Linux 稳定名指着 4.16336.0。按数字比它是最大的(4.16336 > 4.4212 > 4.29),
    # 按字典序却会排在 4.29.0 / 4.4212.0 后面 ⇒ 用错排序就会把正在用的这版删掉。
    link(apps / "Claude-linux-amd64.deb", "claude/4.16336.0/Claude-linux-amd64.deb")
    d = root / "desktop"
    mk(d / "4.16336.0/darwin-universal-Claude-61c5.zip")
    link(d / "Claude-mac.zip", "4.16336.0/darwin-universal-Claude-61c5.zip")
    for v in ("2.1.308", "2.1.309", "2.1.317"):
        mk(root / f"claude-code-releases/{v}/linux-x64/claude.zst")


def sh(script, env):
    r = subprocess.run(["bash", "-c", f'set -euo pipefail; . "{LIB}"\n{script}'],
                       capture_output=True, text=True, env={**os.environ, **env})
    return r


@pytest.fixture
def site(tmp_path):
    web = tmp_path / "www" / "ccdl"
    build_198_layout(web)
    return tmp_path, web


def env_for(tmp, keep=2):
    return {"MIRROR_WEB_ROOT": str(tmp / "www" / "ccdl"), "MIRROR_DATA_ROOT": str(tmp / "data" / "ccdl"),
            "KEEP_VERSIONS": str(keep)}


def dirs(p):
    return sorted(x.name for x in p.iterdir() if x.is_dir() and not x.is_symlink())


def test_sort_V_orders_claude_versions_numerically(site):
    """sort -V 必须按数字比:4.16336.0 > 4.4212.0 > 4.29.0 > 3.6556.2(字典序会把 4.29.0 排最前)。"""
    tmp, web = site
    r = sh(f'cd "{web}/apps/claude" && ls -d */ | tr -d / | sort -rV', env_for(tmp))
    assert r.stdout.split() == ["4.16336.0", "4.4212.0", "4.29.0", "3.6556.2"], r.stdout


def test_prune_keeps_newest_two_and_pinned(site):
    tmp, web = site
    apps = web / "apps"
    r = sh(f'mirror_prune "{apps}/claude" "$(mirror_pinned "{apps}" claude)"', env_for(tmp))
    assert r.returncode == 0, r.stderr
    # 留:最近 2 个(4.16336.0、4.4212.0)。4.16336.0 同时被钉住。删:4.29.0、3.6556.2
    assert dirs(apps / "claude") == ["4.16336.0", "4.4212.0"], r.stdout
    assert (apps / "Claude-linux-amd64.deb").resolve().exists(), "稳定链接不能断"


def test_pinned_old_version_survives_even_if_not_newest(site):
    """阳性对照:稳定名故意指着最旧的那版(新版校验没过、没切过去)⇒ 那一版必须留。"""
    tmp, web = site
    apps = web / "apps"
    os.remove(apps / "Claude-linux-amd64.deb")
    link(apps / "Claude-linux-amd64.deb", "claude/3.6556.2/Claude-linux-amd64.deb")
    r = sh(f'mirror_prune "{apps}/claude" "$(mirror_pinned "{apps}" claude)"', env_for(tmp))
    assert r.returncode == 0, r.stderr
    assert "3.6556.2" in dirs(apps / "claude") and (apps / "Claude-linux-amd64.deb").resolve().exists()
    assert "保留" in r.stdout


def test_codex_windows_and_mac_series_are_pruned_separately(site):
    """Codex 有两条独立的版本线(商店 26.1027.x / appcast mac-26.1029.x),要各留 2 个 ——
    混在一起排的话,mac- 前缀和数字版本号比大小,会把一整条线删光。"""
    tmp, web = site
    c = web / "apps" / "codex"
    for v in ("26.1020.1.0", "26.1023.5.0"):
        mk(c / f"{v}/Codex-windows-x64.msix")
    for v in ("mac-26.1020.100", "mac-26.1023.51421"):
        mk(c / f"{v}/Codex-mac.dmg")
    pin = f'"$(mirror_pinned "{web}/apps" codex)"'
    r = sh(f'mirror_prune "{c}" {pin} ""; mirror_prune "{c}" {pin} "mac-"', env_for(tmp))
    assert r.returncode == 0, r.stderr
    assert dirs(c) == ["26.1023.5.0", "26.1027.6604.0", "mac-26.1023.51421", "mac-26.1029.61003"], dirs(c)


def test_claude_code_in_use_is_kept(site):
    """2.1.308 不在最近 2 个(2.1.317 / 2.1.309)里,但近 7 天还有人下载 ⇒ 传进保护名单就必须留。"""
    tmp, web = site
    cc = web / "claude-code-releases"
    r = sh(f'mirror_prune "{cc}" "$(printf "2.1.317\\n2.1.308\\n")"', env_for(tmp))
    assert dirs(cc) == ["2.1.308", "2.1.309", "2.1.317"], r.stdout
    # 阳性对照:不传保护名单 ⇒ 2.1.308 被删,证明上面那条留下来是靠保护名单、不是恰好没删
    r = sh(f'mirror_prune "{cc}" "2.1.317"', env_for(tmp))
    assert dirs(cc) == ["2.1.309", "2.1.317"], r.stdout


def test_bad_keep_value_deletes_nothing(site):
    tmp, web = site
    r = sh(f'mirror_prune "{web}/apps/claude" ""', env_for(tmp, keep=0))
    assert r.returncode != 0 and len(dirs(web / "apps" / "claude")) == 4


def test_relocate_moves_to_data_and_leaves_symlink(site, monkeypatch):
    tmp, web = site
    (tmp / "data").mkdir()
    before = sorted(str(p.relative_to(web)) for p in web.rglob("*") if p.is_file() and not p.is_symlink())
    # 台架里 /data 不是真挂载点 ⇒ 用一个假的 mountpoint 放行(真机上它会检查挂载盘到底挂没挂)
    fake = tmp / "bin"; fake.mkdir()
    (fake / "mountpoint").write_text("#!/bin/sh\nexit 0\n"); (fake / "mountpoint").chmod(0o755)
    e = {**env_for(tmp), "PATH": f"{fake}:{os.environ['PATH']}"}
    r = sh("mirror_relocate_to_data", e)
    assert r.returncode == 0, r.stderr + r.stdout
    assert web.is_symlink() and web.resolve() == (tmp / "data" / "ccdl").resolve()
    after = sorted(str(p.relative_to(web)) for p in web.rglob("*") if p.is_file() and not p.is_symlink())
    assert before == after, "搬家后文件要一个不少"
    assert (web / "apps" / "Cursor-mac.dmg").resolve().exists(), "相对软链搬过去后仍然能解析"
    assert not list((tmp / "www").glob("ccdl.old-*")), "搬完要删掉旧目录"
    r2 = sh("mirror_relocate_to_data", e)                       # 幂等:第二次什么都不做
    assert r2.returncode == 0 and "已在挂载盘" in r2.stdout


def test_relocate_refuses_when_data_disk_not_mounted(site):
    """挂载盘没挂上时绝不能搬 —— 否则会搬进根盘上的一个空目录,下次挂盘时被盖住,文件"消失"。"""
    tmp, web = site
    (tmp / "data").mkdir()
    fake = tmp / "bin"; fake.mkdir()
    (fake / "mountpoint").write_text("#!/bin/sh\nexit 1\n"); (fake / "mountpoint").chmod(0o755)
    r = sh("mirror_relocate_to_data", {**env_for(tmp), "PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode != 0 and not web.is_symlink() and "不是挂载点" in r.stderr
