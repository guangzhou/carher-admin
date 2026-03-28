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
		KnownBots: map[string]string{
			"cli_test123": "张三",
			"cli_test456": "李四",
		},
		KnownBotOpenIDs: map[string]string{
			"ou_bot123": "cli_test123",
		},
	}

	result := GenerateOpenclawJSON(input)

	// Validate it's valid JSON
	var cfg map[string]interface{}
	if err := json.Unmarshal([]byte(result), &cfg); err != nil {
		t.Fatalf("Generated invalid JSON: %v", err)
	}

	// Check $include
	if cfg["$include"] != "./carher-config.json" {
		t.Error("Missing or wrong $include")
	}

	// Check model
	agents := cfg["agents"].(map[string]interface{})
	defaults := agents["defaults"].(map[string]interface{})
	model := defaults["model"].(map[string]interface{})
	if model["primary"] != "openrouter/openai/gpt-5.4" {
		t.Errorf("Wrong primary model: %v", model["primary"])
	}

	// Check feishu
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

	// Check knownBots
	kb := feishu["knownBots"].(map[string]interface{})
	if kb["cli_test123"] != "张三" {
		t.Error("Wrong knownBots")
	}
	if len(kb) != 2 {
		t.Errorf("Expected 2 knownBots, got %d", len(kb))
	}

	// Check owners
	dm := feishu["dm"].(map[string]interface{})
	allowFrom := dm["allowFrom"].([]interface{})
	if len(allowFrom) != 2 {
		t.Errorf("Expected 2 owners, got %d", len(allowFrom))
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
