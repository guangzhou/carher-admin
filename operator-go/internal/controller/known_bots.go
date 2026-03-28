package controller

import (
	"context"
	"encoding/json"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	herv1 "github.com/guangzhou/carher-admin/operator-go/api/v1alpha1"
	"github.com/guangzhou/carher-admin/operator-go/internal/metrics"
)

const KnownBotsCMName = "carher-known-bots"

// KnownBotsManager maintains a goroutine-safe cache of knownBots
// and writes them to a shared ConfigMap.
type KnownBotsManager struct {
	Client client.Client

	mu             sync.RWMutex
	bots           map[string]string // appId → name
	botOpenIDs     map[string]string // botOpenId → appId
	dirty          bool
	rebuildPending bool
}

func NewKnownBotsManager(c client.Client) *KnownBotsManager {
	return &KnownBotsManager{
		Client:     c,
		bots:       make(map[string]string),
		botOpenIDs: make(map[string]string),
	}
}

// Get returns the current cached knownBots.
func (m *KnownBotsManager) Get() (map[string]string, map[string]string) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	bots := make(map[string]string, len(m.bots))
	for k, v := range m.bots {
		bots[k] = v
	}
	openIDs := make(map[string]string, len(m.botOpenIDs))
	for k, v := range m.botOpenIDs {
		openIDs[k] = v
	}
	return bots, openIDs
}

// MarkDirty signals that knownBots need rebuilding.
func (m *KnownBotsManager) MarkDirty() {
	m.mu.Lock()
	m.dirty = true
	m.mu.Unlock()
}

// Start runs the background loop that rebuilds knownBots when dirty.
func (m *KnownBotsManager) Start(ctx context.Context) error {
	logger := log.FromContext(ctx).WithName("known-bots")
	logger.Info("Starting knownBots manager")

	// Initial load
	m.rebuild(ctx)

	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			m.mu.RLock()
			dirty := m.dirty
			m.mu.RUnlock()
			if dirty {
				m.rebuild(ctx)
			}
		}
	}
}

func (m *KnownBotsManager) rebuild(ctx context.Context) {
	logger := log.FromContext(ctx).WithName("known-bots")

	m.mu.Lock()
	m.dirty = false
	m.mu.Unlock()

	// List all HerInstances
	var list herv1.HerInstanceList
	if err := m.Client.List(ctx, &list, client.InNamespace(Namespace)); err != nil {
		logger.Error(err, "Failed to list HerInstances for knownBots rebuild")
		m.MarkDirty() // Retry
		return
	}

	bots := make(map[string]string)
	openIDs := make(map[string]string)

	for _, her := range list.Items {
		if her.Spec.Paused {
			continue
		}
		if her.Spec.AppID != "" && her.Spec.Name != "" {
			bots[her.Spec.AppID] = her.Spec.Name
		}
		if her.Spec.BotOpenID != "" && her.Spec.AppID != "" {
			openIDs[her.Spec.BotOpenID] = her.Spec.AppID
		}
	}

	// Update cache
	m.mu.Lock()
	m.bots = bots
	m.botOpenIDs = openIDs
	m.mu.Unlock()

	// Update shared ConfigMap
	botsJSON, _ := json.Marshal(bots)
	openIDsJSON, _ := json.Marshal(openIDs)

	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      KnownBotsCMName,
			Namespace: Namespace,
			Labels: map[string]string{
				"app":        "carher",
				"managed-by": "carher-operator",
			},
		},
		Data: map[string]string{
			"knownBots.json":       string(botsJSON),
			"knownBotOpenIds.json": string(openIDsJSON),
		},
	}

	var existing corev1.ConfigMap
	err := m.Client.Get(ctx, types.NamespacedName{Name: KnownBotsCMName, Namespace: Namespace}, &existing)
	if errors.IsNotFound(err) {
		m.Client.Create(ctx, cm)
	} else if err == nil {
		existing.Data = cm.Data
		existing.Labels = cm.Labels
		m.Client.Update(ctx, &existing)
	}

	metrics.KnownBotsCount.Set(float64(len(bots)))
	logger.Info("knownBots rebuilt", "bots", len(bots), "openIDs", len(openIDs))
}
