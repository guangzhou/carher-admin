package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// HerInstanceSpec defines the desired state.
type HerInstanceSpec struct {
	UserID       int    `json:"userId"`
	Name         string `json:"name"`
	Model        string `json:"model,omitempty"`
	AppID        string `json:"appId"`
	AppSecretRef string `json:"appSecretRef,omitempty"`
	Prefix       string `json:"prefix,omitempty"`
	Owner        string `json:"owner,omitempty"`
	Provider     string `json:"provider,omitempty"`
	BotOpenID    string `json:"botOpenId,omitempty"`
	DeployGroup  string `json:"deployGroup,omitempty"`
	Image        string `json:"image,omitempty"`
	Paused       bool   `json:"paused,omitempty"`
}

// HerInstanceStatus defines the observed state.
type HerInstanceStatus struct {
	Phase           string `json:"phase,omitempty"`
	PodIP           string `json:"podIP,omitempty"`
	Node            string `json:"node,omitempty"`
	Restarts        int32  `json:"restarts,omitempty"`
	FeishuWS        string `json:"feishuWS,omitempty"`
	MemoryDB        bool   `json:"memoryDB,omitempty"`
	LastHealthCheck string `json:"lastHealthCheck,omitempty"`
	Message         string `json:"message,omitempty"`
	ConfigHash      string `json:"configHash,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
type HerInstance struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   HerInstanceSpec   `json:"spec,omitempty"`
	Status HerInstanceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type HerInstanceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []HerInstance `json:"items"`
}

func (h *HerInstance) DeepCopyObject() interface{} {
	cp := *h
	cp.Spec = h.Spec
	cp.Status = h.Status
	h.ObjectMeta.DeepCopyInto(&cp.ObjectMeta)
	return &cp
}

func (h *HerInstanceList) DeepCopyObject() interface{} {
	cp := *h
	if h.Items != nil {
		cp.Items = make([]HerInstance, len(h.Items))
		for i := range h.Items {
			cp.Items[i] = *h.Items[i].DeepCopyObject().(*HerInstance)
		}
	}
	return &cp
}

// DeepCopy returns a deep copy of HerInstanceStatus.
func (s *HerInstanceStatus) DeepCopy() *HerInstanceStatus {
	cp := *s
	return &cp
}
