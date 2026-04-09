export const PROVIDER_MODELS = {
  openrouter: [
    { value: "gpt", label: "GPT-5.4" },
    { value: "sonnet", label: "Claude Sonnet 4.6" },
    { value: "opus", label: "Claude Opus 4.6" },
    { value: "gemini", label: "Gemini 3.1 Pro" },
  ],
  anthropic: [
    { value: "sonnet", label: "Claude Sonnet 4.6" },
    { value: "opus", label: "Claude Opus 4.6" },
  ],
  wangsu: [
    { value: "gpt", label: "GPT-5.4" },
    { value: "sonnet", label: "Claude Sonnet 4.6" },
    { value: "opus", label: "Claude Opus 4.6" },
    { value: "gemini", label: "Gemini 3.1 Pro" },
  ],
  litellm: [
    { value: "gpt", label: "GPT-5.4" },
    { value: "sonnet", label: "Claude Sonnet 4.6" },
    { value: "opus", label: "Claude Opus 4.6" },
    { value: "gemini", label: "Gemini 3.1 Pro" },
  ],
};

export const ALL_MODELS = [
  { value: "gpt", label: "GPT-5.4" },
  { value: "sonnet", label: "Claude Sonnet 4.6" },
  { value: "opus", label: "Claude Opus 4.6" },
  { value: "gemini", label: "Gemini 3.1 Pro" },
];

// Mirrors config_gen.py MODEL_MAP_* alias resolution
const MODEL_ALIAS = {
  wangsu:     { sonnet: "ws-sonnet", opus: "ws-opus", gpt: "ws-gpt", gemini: "ws-gemini" },
  openrouter: { sonnet: "sonnet", opus: "opus", gpt: "gpt", gemini: "gemini" },
  anthropic:  { sonnet: "sonnet", opus: "opus", gpt: "gpt" },
  litellm:    { sonnet: "sonnet", opus: "opus", gpt: "gpt", gemini: "gemini" },
};

export function getModelAlias(provider, modelShort) {
  return MODEL_ALIAS[provider]?.[modelShort] || modelShort || "-";
}
