package controller

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestGenerateOpenclawJSON_Basic(t *testing.T) {
	input := ConfigInput{
		ID:        14,
		Name:      "张三",
		Model:     "gpt",
		AppID:     "cli_test123",
		AppSecret: "secret123",
		Prefix:    "s3",
		Owner:     "ou_abc|ou_def",
		Provider:  "openrouter",
		BotOpenID: "ou_bot123",
	}

	result := GenerateOpenclawJSON(input)

	var cfg map[string]interface{}
	if err := json.Unmarshal([]byte(result), &cfg); err != nil {
		t.Fatalf("Generated invalid JSON: %v", err)
	}

	if cfg["$include"] != "./carher-config.json" {
		t.Error("Missing or wrong $include")
	}

	agents := cfg["agents"].(map[string]interface{})
	defaults := agents["defaults"].(map[string]interface{})
	model := defaults["model"].(map[string]interface{})
	if model["primary"] != "openrouter/openai/gpt-5.4" {
		t.Errorf("Wrong primary model: %v", model["primary"])
	}

	channels := cfg["channels"].(map[string]interface{})
	feishu := channels["feishu"].(map[string]interface{})
	if feishu["appId"] != "cli_test123" {
		t.Error("Wrong appId")
	}
	if !strings.Contains(feishu["oauthRedirectUri"].(string), "s3-u14-auth.carher.net") {
		t.Errorf("Wrong OAuth URL: %v", feishu["oauthRedirectUri"])
	}
	if feishu["botOpenId"] != "ou_bot123" {
		t.Error("Missing botOpenId")
	}
	if feishu["name"] != "张三的her" {
		t.Errorf("Expected name suffix '的her', got: %v", feishu["name"])
	}

	// knownBots should NOT be present (now via Redis)
	if _, ok := feishu["knownBots"]; ok {
		t.Error("knownBots should not be in feishu config (now dynamic via Redis)")
	}

	dm := feishu["dm"].(map[string]interface{})
	allowFrom := dm["allowFrom"].([]interface{})
	if len(allowFrom) != 2 {
		t.Errorf("Expected 2 owners, got %d", len(allowFrom))
	}

	// Check minimax model uses m2.7
	models := defaults["models"].(map[string]interface{})
	if _, ok := models["openrouter/minimax/minimax-m2.7"]; !ok {
		t.Error("Expected minimax-m2.7 model key")
	}
	if _, ok := models["openrouter/minimax/minimax-m2.5"]; ok {
		t.Error("Old minimax-m2.5 should not be present")
	}

	// Check Google/Anthropic routing on openrouter opus
	opusModel := models["openrouter/anthropic/claude-opus-4.6"].(map[string]interface{})
	if _, ok := opusModel["params"]; !ok {
		t.Error("openrouter opus should have Google/Anthropic routing params")
	}
}

func TestGenerateOpenclawJSON_Anthropic(t *testing.T) {
	input := ConfigInput{
		ID:        1,
		Name:      "Test",
		Model:     "opus",
		AppID:     "cli_a",
		AppSecret: "s",
		Prefix:    "s1",
		Provider:  "anthropic",
	}

	result := GenerateOpenclawJSON(input)
	var cfg map[string]interface{}
	json.Unmarshal([]byte(result), &cfg)

	agents := cfg["agents"].(map[string]interface{})
	defaults := agents["defaults"].(map[string]interface{})
	model := defaults["model"].(map[string]interface{})
	if model["primary"] != "anthropic/claude-opus-4-6" {
		t.Errorf("Wrong anthropic model: %v", model["primary"])
	}

	models := defaults["models"].(map[string]interface{})
	opus := models["anthropic/claude-opus-4-6"].(map[string]interface{})
	if opus["alias"] != "opus" {
		t.Error("Anthropic opus should have primary alias")
	}

	// openrouter models should have routing when provider=anthropic
	orOpus := models["openrouter/anthropic/claude-opus-4.6"].(map[string]interface{})
	if _, ok := orOpus["params"]; !ok {
		t.Error("openrouter opus (anthropic provider) should have routing params")
	}
}

func TestGenerateOpenclawJSON_NoFeishu(t *testing.T) {
	input := ConfigInput{
		ID:    99,
		Name:  "NoFeishu",
		Model: "gpt",
	}

	result := GenerateOpenclawJSON(input)
	var cfg map[string]interface{}
	json.Unmarshal([]byte(result), &cfg)

	if _, ok := cfg["channels"]; ok {
		t.Error("Should not have channels when appId is empty")
	}
}

func TestGenerateOpenclawJSON_OAuthRedirectOverride(t *testing.T) {
	input := ConfigInput{
		ID:               14,
		Name:             "Test",
		Model:            "opus",
		AppID:            "cli_test",
		AppSecret:        "secret",
		Prefix:           "s3",
		Provider:         "wangsu",
		OAuthRedirectUri: "https://s3-u9999-auth.carher.net/feishu/oauth/callback",
	}

	result := GenerateOpenclawJSON(input)
	var cfg map[string]interface{}
	json.Unmarshal([]byte(result), &cfg)

	channels := cfg["channels"].(map[string]interface{})
	feishu := channels["feishu"].(map[string]interface{})
	uri := feishu["oauthRedirectUri"].(string)
	if uri != "https://s3-u9999-auth.carher.net/feishu/oauth/callback" {
		t.Errorf("Expected override URI, got: %v", uri)
	}
}

func TestGenerateOpenclawJSON_Litellm(t *testing.T) {
	input := ConfigInput{
		ID:         1000,
		Name:       "Test",
		Model:      "opus",
		AppID:      "cli_test",
		AppSecret:  "secret",
		Prefix:     "s1",
		Provider:   "litellm",
		LitellmKey: "sk-test-key",
	}

	result := GenerateOpenclawJSON(input)
	var cfg map[string]interface{}
	if err := json.Unmarshal([]byte(result), &cfg); err != nil {
		t.Fatalf("Generated invalid JSON: %v", err)
	}

	agents := cfg["agents"].(map[string]interface{})
	defaults := agents["defaults"].(map[string]interface{})
	model := defaults["model"].(map[string]interface{})
	if model["primary"] != "litellm/claude-opus-4-6" {
		t.Errorf("Wrong primary model: %v", model["primary"])
	}

	models := defaults["models"].(map[string]interface{})

	expectedAliases := map[string]string{
		"litellm/claude-opus-4-6":      "opus",
		"litellm/claude-sonnet-4-6":    "sonnet",
		"litellm/gpt-5.4":             "gpt",
		"litellm/gemini-3.1-pro-preview": "gemini",
		"litellm/minimax-m2.7":        "minimax",
		"litellm/glm-5":               "glm",
		"litellm/gpt-5.3-codex":       "codex",
	}
	for mid, wantAlias := range expectedAliases {
		m, ok := models[mid]
		if !ok {
			t.Errorf("Missing model %s", mid)
			continue
		}
		got := m.(map[string]interface{})["alias"]
		if got != wantAlias {
			t.Errorf("Model %s alias = %v, want %v", mid, got, wantAlias)
		}
	}

	if len(models) != 7 {
		t.Errorf("Expected exactly 7 models for litellm, got %d", len(models))
		for k := range models {
			t.Logf("  model: %s", k)
		}
	}

	// No openrouter/* models should be present
	for k := range models {
		if strings.HasPrefix(k, "openrouter/") {
			t.Errorf("Unexpected openrouter model in litellm config: %s", k)
		}
	}

	// Verify litellm provider is defined with 7 models
	modelsSection := cfg["models"].(map[string]interface{})
	providers := modelsSection["providers"].(map[string]interface{})
	litellmProv := providers["litellm"].(map[string]interface{})
	provModels := litellmProv["models"].([]interface{})
	if len(provModels) != 7 {
		t.Errorf("Expected 7 provider models, got %d", len(provModels))
	}

	if litellmProv["apiKey"] != "sk-test-key" {
		t.Errorf("Wrong apiKey: %v", litellmProv["apiKey"])
	}
}

func TestGenerateOpenclawJSON_NameSuffix(t *testing.T) {
	tests := []struct {
		inputName string
		wantName  string
	}{
		{"张三", "张三的her"},
		{"永兵的her", "永兵的her"},
		{"国际法务的Her", "国际法务的Her"},
		{"国现的her(阿里云)", "国现的her(阿里云)"},
		{"IT基础设施her", "IT基础设施her"},
		{"", ""},
	}

	for _, tt := range tests {
		input := ConfigInput{
			ID: 1, Model: "gpt", AppID: "cli_a", AppSecret: "s",
			Prefix: "s1", Provider: "litellm", Name: tt.inputName,
		}
		result := GenerateOpenclawJSON(input)
		var cfg map[string]interface{}
		json.Unmarshal([]byte(result), &cfg)

		if tt.inputName == "" {
			if _, ok := cfg["channels"]; ok {
				feishu := cfg["channels"].(map[string]interface{})["feishu"].(map[string]interface{})
				if feishu["name"] != "" {
					t.Errorf("Name=%q: expected empty feishu name, got %v", tt.inputName, feishu["name"])
				}
			}
			continue
		}

		channels := cfg["channels"].(map[string]interface{})
		feishu := channels["feishu"].(map[string]interface{})
		got := feishu["name"].(string)
		if got != tt.wantName {
			t.Errorf("Name=%q: feishu name = %q, want %q", tt.inputName, got, tt.wantName)
		}
	}
}

func TestSplitOwners(t *testing.T) {
	tests := []struct {
		input    string
		expected int
	}{
		{"ou_abc|ou_def", 2},
		{"ou_abc", 1},
		{"", 0},
		{"ou_abc||ou_def|", 2},
		{"  ou_abc  |  ou_def  ", 2},
	}

	for _, tt := range tests {
		result := splitOwners(tt.input)
		if len(result) != tt.expected {
			t.Errorf("splitOwners(%q) = %d items, want %d", tt.input, len(result), tt.expected)
		}
	}
}
