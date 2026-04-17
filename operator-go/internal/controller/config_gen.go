package controller

import (
	"encoding/json"
	"fmt"
	"strings"
)

const (
	GeminiProject = "gen-lang-client-0519229117"
	GeminiModel   = "gemini-live-2.5-flash-native-audio"
)

var modelMap = map[string]string{
	"sonnet": "openrouter/anthropic/claude-sonnet-4.6",
	"opus":   "openrouter/anthropic/claude-opus-4.6",
	"gpt":    "openrouter/openai/gpt-5.4",
	"gemini": "openrouter/google/gemini-3.1-pro-preview",
}

var modelMapAnthropic = map[string]string{
	"sonnet": "anthropic/claude-sonnet-4-6",
	"opus":   "anthropic/claude-opus-4-6",
	"gpt":    "openrouter/openai/gpt-5.4",
}

var modelMapWangsu = map[string]string{
	"sonnet": "wangsu/claude-sonnet-4-6",
	"opus":   "wangsu/claude-opus-4-6",
	"gpt":    "wangsu/gpt-5.4",
	"gemini": "wangsu/gemini-3.1-pro-preview",
}

var modelMapLitellm = map[string]string{
	"sonnet":  "litellm/claude-sonnet-4-6",
	"opus":    "litellm/claude-opus-4-6",
	"gpt":     "litellm/gpt-5.4",
	"gemini":  "litellm/gemini-3.1-pro-preview",
	"minimax": "litellm/minimax-m2.7",
	"glm":     "litellm/glm-5",
	"codex":   "litellm/gpt-5.3-codex",
}

// extraLitellmModelRegistry holds metadata for opt-in LiteLLM models that are
// NOT part of the default set. Keyed by the proxy-side model_name (as defined
// in k8s/litellm-proxy.yaml ConfigMap). When a HerInstance has annotation
// `carher.io/extra-litellm-models=<id1,id2>`, each listed id looked up here
// gets merged into that instance's generated config — only that instance.
// Unknown ids are silently skipped so typos don't brick the bot.
//
// Empty for now — `anthropic.claude-opus-4-7` graduated to the default set
// (see case "litellm" below). Add new opt-in models here to stage rollouts.
var extraLitellmModelRegistry = map[string]map[string]interface{}{}

// Google Vertex provider routing: prefer Google → Anthropic fallback
var googleAnthropicRouting = map[string]interface{}{
	"params": map[string]interface{}{
		"provider": map[string]interface{}{
			"order":          []string{"Google", "Anthropic"},
			"allow_fallbacks": true,
		},
	},
}

type ConfigInput struct {
	ID                 int
	Name               string
	Model              string
	AppID              string
	AppSecret          string
	Prefix             string
	Owner              string
	Provider           string
	LitellmKey         string
	BotOpenID          string
	OAuthRedirectUri   string
	ExtraLitellmModels []string
}

func resolveOAuthRedirectUri(override string, pfx string, id int) string {
	if override != "" {
		return override
	}
	return fmt.Sprintf("https://%su%d-auth.carher.net/feishu/oauth/callback", pfx, id)
}

func GenerateOpenclawJSON(input ConfigInput) string {
	prefix := resolvePrefix(input.Prefix)
	pfx := prefix + "-"

	mm := modelMap
	switch input.Provider {
	case "litellm":
		mm = modelMapLitellm
	case "wangsu":
		mm = modelMapWangsu
	case "anthropic":
		mm = modelMapAnthropic
	}
	modelFull := mm[input.Model]
	if modelFull == "" {
		modelFull = input.Model
	}

	alias := func(a string) map[string]interface{} { return map[string]interface{}{"alias": a} }
	aliasWithRouting := func(a string) map[string]interface{} {
		m := map[string]interface{}{"alias": a}
		for k, v := range googleAnthropicRouting {
			m[k] = v
		}
		return m
	}

	models := make(map[string]interface{})
	switch input.Provider {
	case "litellm":
		models["litellm/claude-opus-4-6"] = alias("opus")
		models["litellm/claude-sonnet-4-6"] = alias("sonnet")
		models["litellm/gpt-5.4"] = alias("gpt")
		models["litellm/gemini-3.1-pro-preview"] = alias("gemini")
		models["litellm/minimax-m2.7"] = alias("minimax")
		models["litellm/glm-5"] = alias("glm")
		models["litellm/gpt-5.3-codex"] = alias("codex")
		models["litellm/anthropic.claude-opus-4-7"] = alias("opus4.7")
	case "anthropic":
		models["anthropic/claude-opus-4-6"] = alias("opus")
		models["anthropic/claude-sonnet-4-6"] = alias("sonnet")
		models["openrouter/anthropic/claude-opus-4.6"] = aliasWithRouting("or-opus")
		models["openrouter/anthropic/claude-sonnet-4.6"] = aliasWithRouting("or-sonnet")
	default:
		models["openrouter/anthropic/claude-opus-4.6"] = aliasWithRouting("opus")
		models["openrouter/anthropic/claude-sonnet-4.6"] = aliasWithRouting("sonnet")
		models["anthropic/claude-opus-4-6"] = alias("or-opus")
		models["anthropic/claude-sonnet-4-6"] = alias("or-sonnet")
	}
	if input.Provider != "litellm" {
		models["openrouter/google/gemini-3.1-pro-preview"] = alias("gemini")
		models["openrouter/minimax/minimax-m2.7"] = alias("minimax")
		models["openrouter/z-ai/glm-5"] = alias("glm")
		models["openrouter/openai/gpt-5.4"] = alias("gpt")
		models["openrouter/openai/gpt-5.3-codex"] = alias("codex")
	}
	if input.Provider == "wangsu" {
		models["wangsu/claude-opus-4-6"] = alias("ws-opus")
		models["wangsu/claude-sonnet-4-6"] = alias("ws-sonnet")
		models["wangsu/gpt-5.4"] = alias("ws-gpt")
		models["wangsu/gemini-3.1-pro-preview"] = alias("ws-gemini")
	}

	// Opt-in extra LiteLLM models (per-instance via annotation).
	// Only merged when provider=litellm; unknown ids skipped.
	extraProviderModels := []map[string]interface{}{}
	if input.Provider == "litellm" {
		for _, id := range input.ExtraLitellmModels {
			meta, ok := extraLitellmModelRegistry[id]
			if !ok {
				continue
			}
			a, _ := meta["alias"].(string)
			if a == "" {
				a = id
			}
			// agents.defaults.models uses "litellm/<proxy_model_name>" as key
			models["litellm/"+id] = alias(a)
			// providers.litellm.models list uses <proxy_model_name> as id (no prefix)
			entry := make(map[string]interface{}, len(meta))
			for k, v := range meta {
				if k == "alias" {
					continue
				}
				entry[k] = v
			}
			extraProviderModels = append(extraProviderModels, entry)
		}
	}

	cfg := map[string]interface{}{
		"$include": "./carher-config.json",
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{
				"model": map[string]interface{}{
					"primary": modelFull,
				},
				"models": models,
			},
		},
		"plugins": map[string]interface{}{
			"entries": map[string]interface{}{
				"realtime": map[string]interface{}{
					"config": map[string]interface{}{
						"gemini": map[string]string{
							"projectId": GeminiProject,
							"model":     GeminiModel,
						},
					},
				},
			},
		},
	}

	if input.Provider == "litellm" {
		apiKey := "${LITELLM_API_KEY}"
		if input.LitellmKey != "" {
			apiKey = input.LitellmKey
		}
		baseModels := []map[string]interface{}{
			{"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "api": "openai-completions", "reasoning": true, "input": []string{"text", "image"}, "contextWindow": 200000, "maxTokens": 128000, "cost": map[string]interface{}{"input": 5, "output": 25, "cacheRead": 0.5}},
			{"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "api": "openai-completions", "reasoning": true, "input": []string{"text", "image"}, "contextWindow": 200000, "maxTokens": 64000, "cost": map[string]interface{}{"input": 3, "output": 15, "cacheRead": 0.3}},
			{"id": "gpt-5.4", "name": "GPT-5.4", "api": "openai-completions", "reasoning": true, "input": []string{"text", "image"}, "contextWindow": 200000, "maxTokens": 128000, "cost": map[string]interface{}{"input": 2.5, "output": 15, "cacheRead": 0.25}},
			{"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro", "api": "openai-completions", "reasoning": true, "input": []string{"text", "image"}, "contextWindow": 200000, "maxTokens": 65536, "cost": map[string]interface{}{"input": 2, "output": 12, "cacheRead": 0.2}},
			{"id": "minimax-m2.7", "name": "MiniMax M2.7", "api": "openai-completions", "input": []string{"text"}, "contextWindow": 200000, "maxTokens": 128000, "cost": map[string]interface{}{"input": 0.5, "output": 1.5}},
			{"id": "glm-5", "name": "GLM-5", "api": "openai-completions", "input": []string{"text"}, "contextWindow": 128000, "maxTokens": 32000, "cost": map[string]interface{}{"input": 1, "output": 3}},
			{"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex", "api": "openai-completions", "reasoning": true, "input": []string{"text"}, "contextWindow": 200000, "maxTokens": 128000, "cost": map[string]interface{}{"input": 3, "output": 15}},
			{"id": "anthropic.claude-opus-4-7", "name": "Claude Opus 4.7", "api": "openai-completions", "reasoning": true, "input": []string{"text", "image"}, "contextWindow": 200000, "maxTokens": 128000, "cost": map[string]interface{}{"input": 5, "output": 25, "cacheRead": 0.5}},
		}
		providerModels := append(baseModels, extraProviderModels...)
		cfg["models"] = map[string]interface{}{
			"providers": map[string]interface{}{
				"litellm": map[string]interface{}{
					"baseUrl": "http://litellm-proxy.carher.svc:4000",
					"apiKey":  apiKey,
					"models":  providerModels,
				},
			},
		}
	}

	owners := splitOwners(input.Owner)

	if input.AppID != "" && input.AppSecret != "" {
		feishuName := input.Name
		if feishuName != "" && !strings.Contains(strings.ToLower(feishuName), "的her") {
			feishuName += "的her"
		}
		feishu := map[string]interface{}{
			"enabled":          true,
			"appId":            input.AppID,
			"appSecret":        input.AppSecret,
			"name":             feishuName,
			"groups":           map[string]bool{"enabled": true, "archive": true},
			"oauthRedirectUri": resolveOAuthRedirectUri(input.OAuthRedirectUri, pfx, input.ID),
		}
		// knownBots/knownBotOpenIds removed — now populated dynamically via Redis bot-registry.
		if input.BotOpenID != "" {
			feishu["botOpenId"] = input.BotOpenID
		}
		if len(owners) > 0 {
			feishu["dm"] = map[string]interface{}{"allowFrom": owners}
		}
		cfg["channels"] = map[string]interface{}{"feishu": feishu}
	}

	if len(owners) > 0 {
		cfg["commands"] = map[string]interface{}{"ownerAllowFrom": owners}
	}

	b, _ := json.MarshalIndent(cfg, "", "  ")
	return string(b)
}
