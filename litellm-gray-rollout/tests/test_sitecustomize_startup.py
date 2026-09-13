"""Startup compatibility checks for callbacks mounted before LiteLLM boots."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SITECUSTOMIZE = ROOT / "k8s" / "litellm-callbacks" / "sitecustomize.py"


def test_sitecustomize_adds_app_before_importing_sibling_callback():
    source = SITECUSTOMIZE.read_text(encoding="utf-8")

    path_insert = source.index('sys.path.insert(0, _APP_DIR)')
    callback_import = source.index("import responses_aclose")

    assert '_APP_DIR = "/app"' in source
    assert path_insert < callback_import
