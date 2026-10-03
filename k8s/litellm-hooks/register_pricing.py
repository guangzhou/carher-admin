"""
Startup hook: inject custom model pricing into litellm.model_cost
for models not in the built-in cost map.
"""
import litellm

CUSTOM_MODELS = {
    "anthropic.claude-opus-4-6": {
        "input_cost_per_token": 0.000005,
        "output_cost_per_token": 0.000025,
        "cache_read_input_token_cost": 0.0000005,
        "cache_creation_input_token_cost": 0.00000625,
        "litellm_provider": "anthropic",
        "mode": "chat",
        "max_tokens": 128000,
        "max_input_tokens": 1000000,
        "max_output_tokens": 128000,
    },
}

def register_custom_pricing():
    litellm.register_model(CUSTOM_MODELS)
