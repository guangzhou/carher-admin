from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
FALLBACK_GROUP = "chatgpt-budget-fallback-gpt-5.3"


def load_litellm_config() -> dict:
    documents = yaml.safe_load_all(
        (ROOT / "k8s/litellm-proxy.yaml").read_text(encoding="utf-8")
    )
    configmap = next(
        item
        for item in documents
        if item
        and item.get("kind") == "ConfigMap"
        and item.get("metadata", {}).get("name") == "litellm-config"
    )
    return yaml.safe_load(configmap["data"]["config.yaml"])


def test_carher_proxy_does_not_declare_cross_cluster_budget_fallback_rows():
    config = load_litellm_config()
    rows = [
        row for row in config["model_list"] if row["model_name"] == FALLBACK_GROUP
    ]

    assert rows == []


def test_budget_fallback_group_has_no_paid_router_fallback():
    config = load_litellm_config()
    sources = {
        next(iter(item)): item[next(iter(item))]
        for item in config["router_settings"].get("fallbacks", [])
    }

    assert FALLBACK_GROUP not in sources
