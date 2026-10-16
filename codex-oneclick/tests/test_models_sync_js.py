"""Node 版模型同步(carher_models_sync.js)与 Python 版逐项一致(2026-10-16 加)。

为什么有 Node 版:Windows 同事没装 Python ⇒ 同步从没跑过 ⇒ Codex App 下拉里找不到 key 里唯一的 deepseek。
两份逻辑一旦漂移,有 Python 和没 Python 的人会看到不同的菜单,且不会有人来报 ⇒ 用同一组输入逐字段比对。
"""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
spec = importlib.util.spec_from_file_location("carher_models_sync", SRC / "carher_models_sync.py")
py = importlib.util.module_from_spec(spec)
spec.loader.exec_module(py)

from test_carher_models_sync import TEMPLATES  # noqa: E402  同一组官方模板


def js(fn, *args):
    code = (f"const m=require({json.dumps(str(SRC / 'carher_models_sync.js'))});"
            f"process.stdout.write(JSON.stringify(m.{fn}(...{json.dumps(list(args))})))")
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


@pytest.mark.parametrize("ids,limits", [
    (["deepseek-v4.1-flash"], {}),                                   # 10-16 fenglili 那把 key
    (["gpt-5.4", "grok-4.7", "gpt-image-2", "codex-auto-review"], {}),
    (["claude-haiku-4-5", "jev", "gpt-5.6-terra"], {"claude-haiku-4-5": 200000, "jev": 1000000}),
])
def test_build_catalog_identical(ids, limits):
    assert js("buildCatalog", ids, TEMPLATES, limits) == py.build_catalog(ids, TEMPLATES, limits)


def test_only_deepseek_key_shows_deepseek_in_picker():
    cat = js("buildCatalog", ["deepseek-v4.1-flash"], TEMPLATES, {})
    shown = [m["slug"] for m in cat["models"] if m["visibility"] == "list"]
    assert shown == ["deepseek-v4.1-flash"]


@pytest.mark.parametrize("text", [
    'model = "x"\n\n[model_providers.p]\nname = "p"\n',
    'model = "x"\n',
])
def test_config_line_identical(text):
    p = r"C:\Users\张三\.codex\carher-models.json"
    assert js("ensureConfigLine", text, p) == py.ensure_config_line(text, Path(p))
    assert js("catalogPathFromConfig", py.ensure_config_line(text, Path(p))) == str(Path(p))


def test_installers_fall_back_to_node_when_no_python():
    mac = (SRC / "install-mac.sh").read_text(encoding="utf-8")
    win = (SRC / "install-windows.ps1").read_text(encoding="utf-8-sig")
    assert "@@EMBED carher_models_sync.js@@" in mac and "@@EMBED carher_models_sync.js@@" in win
    assert 'SYNC_ARG="$CODEX_HOME/carher_models_sync.js"' in mac
    assert "$syncArg = $syncJs" in win


def test_mac_step7_uses_node(tmp_path):
    """切出安装脚本第 7 步真跑:PYBIN 为空 ⇒ 必须用 node 跑 .js(10-16 那台机器的形状)。"""
    import re
    mac = (SRC / "install-mac.sh").read_text(encoding="utf-8")
    helpers = "\n".join(l for l in mac.splitlines() if re.match(r"^(BLUE|say|ok|warn|die)", l))
    step7 = re.search(r"^# --- 7\..*?(?=^echo\nsay \"=)", mac, re.S | re.M).group(0)
    # 只验「选哪个解释器跑哪个文件」,不碰 launchd:把 uname 改成非 Darwin、crontab 不存在
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "node").write_text('#!/bin/sh\necho "RAN node $1"\n')
    (fake / "uname").write_text("#!/bin/sh\necho Linux\n")
    for f in fake.iterdir():
        f.chmod(0o755)
    script = helpers + f'\nCODEX_HOME="{tmp_path}"\nPYBIN=""\nNODEBIN="{fake}/node"\n' + step7
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{fake}:/bin", "HOME": str(tmp_path)}, timeout=30)
    assert f"RAN node {tmp_path}/carher_models_sync.js" in r.stdout, r.stdout + r.stderr
    assert "已同步模型列表" in r.stdout


def test_mac_step7_prefers_node_even_with_python(tmp_path):
    """10-16:Python 版要靠 codex 命令拿模板,定时任务 PATH 里没有 node ⇒ 静默失败;有 Node 就一律用 Node 版。"""
    import re
    mac = (SRC / "install-mac.sh").read_text(encoding="utf-8")
    helpers = "\n".join(l for l in mac.splitlines() if re.match(r"^(BLUE|say|ok|warn|die)", l))
    step7 = re.search(r"^# --- 7\..*?(?=^echo\nsay \"=)", mac, re.S | re.M).group(0)
    fake = tmp_path / "bin"; fake.mkdir()
    (fake / "node").write_text('#!/bin/sh\necho "RAN node $1"\n')
    (fake / "py").write_text('#!/bin/sh\necho "RAN py $1"\n')
    (fake / "uname").write_text("#!/bin/sh\necho Linux\n")
    for f in fake.iterdir(): f.chmod(0o755)
    script = helpers + f'\nCODEX_HOME="{tmp_path}"\nPYBIN="{fake}/py"\nNODEBIN="{fake}/node"\n' + step7
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{fake}:/bin", "HOME": str(tmp_path)}, timeout=30)
    assert "RAN node" in r.stdout and "RAN py" not in r.stdout, r.stdout + r.stderr


def test_image_tool_node_first_in_both_installers():
    mac = (SRC / "install-mac.sh").read_text(encoding="utf-8")
    win = (SRC / "install-windows.ps1").read_text(encoding="utf-8-sig")
    assert "@@EMBED carher_image.js@@" in mac and "@@EMBED carher_image.js@@" in win
    assert mac.index('IMG_CMD="\\"$NODEBIN\\"') < mac.index('IMG_CMD="\\"$PYBIN\\"')
    assert "if ($nodeExe) { $imgCmd" in win
