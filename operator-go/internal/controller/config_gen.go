package controller

import (
	"encoding/json"
	"fmt"
)

const (
	GeminiProject = "gen-lang-client-0519229117"
	GeminiModel   = "gemini-live-2.5-flash-native-audio"
)

var modelMap = map[string]string{
	"sonnet": "openrouter/anthropic/claude-sonnet-4.6",
	"opus":   "openrouter/anthropic/claude-opus-4.6",
	"gpt":    "openrouter/openai/gpt-5.4",
}

var modelMapAnthropic = map[string]string{
	"sonnet": "anthropic/claude-sonnet-4-6",
	"opus":   "anthropic/claude-opus-4-6",
	"gpt":    "openrouter/openai/gpt-5.4",
}

type ConfigInput struct {
	ID              int
	Name            string
	Model           string
	AppID           string
	AppSecret       string
	Prefix          string
	Owner           string
	Provider        string
	BotOpenID       string
	KnownBots       map[string]string
	KnownBotOpenIDs map[string]string
}

func GenerateOpenclawJSON(input ConfigInput) string {
	prefix := input.Prefix
	if prefix == "" {
		prefix = "s1"
	}
	pfx := prefix + "-"

	mm := modelMap
	if input.Provider == "anthropic" {
		mm = modelMapAnthropic
	}
	modelFull := mm[input.Model]
	if modelFull == "" {
		modelFull = input.Model
	}

	alias := func(a string) map[string]string { return map[string]string{"alias": a} }

	models := make(map[string]interface{})
	if input.Provider == "anthropic" {
		models["anthropic/claude-opus-4-6"] = alias("opus")
		models["anthropic/claude-sonnet-4-6"] = alias("sonnet")
		models["openrouter/anthropic/claude-opus-4.6"] = alias("or-opus")
		models["openrouter/anthropic/claude-sonnet-4.6"] = alias("or-sonnet")
	} else {
		models["openrouter/anthropic/claude-opus-4.6"] = alias("opus")
		models["openrouter/anthropic/claude-sonnet-4.6"] = alias("sonnet")
		models["anthropic/claude-opus-4-6"] = alias("or-opus")
		models["anthropic/claude-sonnet-4-6"] = alias("or-sonnet")
	}
	models["openrouter/google/gemini-3.1-pro-preview"] = alias("gemini")
	models["openrouter/minimax/minimax-m2.5"] = alias("minimax")
	models["openrouter/z-ai/glm-5"] = alias("glm")
	models["openrouter/openai/gpt-5.4"] = alias("gpt")
	models["openrouter/openai/gpt-5.3-codex"] = alias("codex")

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

	owners := splitOwners(input.Owner)

	if input.AppID != "" && input.AppSecret != "" {
		feishu := map[string]interface{}{
			"enabled":          true,
			"appId":            input.AppID,
			"appSecret":        input.AppSecret,
			"name":             input.Name,
			"groups":           map[string]bool{"enabled": true, "archive": true},
			"oauthRedirectUri": fmt.Sprintf("https://%su%d-auth.carher.net/feishu/oauth/callback", pfx, input.ID),
		}
		if len(input.KnownBots) > 0 {
			feishu["knownBots"] = input.KnownBots
		}
		if len(input.KnownBotOpenIDs) > 0 {
			feishu["knownBotOpenIds"] = input.KnownBotOpenIDs
		}
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
