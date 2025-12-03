package kube

import (
	"context"
	"fmt"
	"math/rand"
	"strconv"
	"strings"
	"time"

	"github.com/fatih/color"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
	metricsv "k8s.io/metrics/pkg/client/clientset/versioned"
)

const (
	// PodDeleteMaxWait is the multiplier for wait timeout during pod deletion
	PodDeleteMaxWait = 2
)

// Client wraps the Kubernetes client
type Client struct {
	clientset     *kubernetes.Clientset
	metricsClient *metricsv.Clientset
	debug         bool
}

// PodMetrics holds CPU and memory usage for a pod
type PodMetrics struct {
	CPU int64
	Mem int64
}

// ControllerStatus holds the status of a controller
type ControllerStatus struct {
	Want        int32
	Ready       int32
	Available   int32
	WaitTimeout int32
}

// NewClient creates a new Kubernetes client
func NewClient() (*Client, error) {
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	configOverrides := &clientcmd.ConfigOverrides{}
	kubeConfig := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loadingRules, configOverrides)

	config, err := kubeConfig.ClientConfig()
	if err != nil {
		return nil, fmt.Errorf("failed to load kubeconfig: %w", err)
	}

	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create clientset: %w", err)
	}

	metricsClient, err := metricsv.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create metrics client: %w", err)
	}

	return &Client{
		clientset:     clientset,
		metricsClient: metricsClient,
		debug:         false,
	}, nil
}

// SetDebug enables or disables debug mode
func (c *Client) SetDebug(debug bool) {
	c.debug = debug
}

// GetWorkerNodes returns all worker nodes (non-control-plane nodes)
func (c *Client) GetWorkerNodes(ctx context.Context) ([]corev1.Node, error) {
	nodeList, err := c.clientset.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("failed to list nodes: %w", err)
	}

	var workers []corev1.Node
	for _, node := range nodeList.Items {
		labels := node.Labels
		if labels == nil {
			labels = make(map[string]string)
		}
		// Worker nodes don't have the control-plane role label
		if _, hasControlPlane := labels["node-role.kubernetes.io/control-plane"]; !hasControlPlane {
			workers = append(workers, node)
		}
	}
	return workers, nil
}

// GetAllPods returns all pods across all namespaces
func (c *Client) GetAllPods(ctx context.Context, ordered bool) ([]corev1.Pod, error) {
	podList, err := c.clientset.CoreV1().Pods("").List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("failed to list pods: %w", err)
	}

	pods := podList.Items
	if !ordered {
		rand.Shuffle(len(pods), func(i, j int) {
			pods[i], pods[j] = pods[j], pods[i]
		})
	}
	return pods, nil
}

// GetPodsOnNodes returns all pods running on the specified nodes
func (c *Client) GetPodsOnNodes(ctx context.Context, nodes []corev1.Node) ([]corev1.Pod, error) {
	nodeNames := make(map[string]bool)
	for _, node := range nodes {
		nodeNames[node.Name] = true
	}

	allPods, err := c.GetAllPods(ctx, false)
	if err != nil {
		return nil, err
	}

	var matchingPods []corev1.Pod
	for _, pod := range allPods {
		if nodeNames[pod.Spec.NodeName] {
			matchingPods = append(matchingPods, pod)
		}
	}
	return matchingPods, nil
}

// calculateMaxProbeTimeout calculates the maximum timeout for a probe
func calculateMaxProbeTimeout(probe *corev1.Probe) int32 {
	if probe == nil {
		return 0
	}
	timeout := probe.InitialDelaySeconds
	timeout += probe.SuccessThreshold * (probe.TimeoutSeconds + probe.PeriodSeconds)
	return timeout
}

// calculateWaitTimeout calculates the wait timeout for a pod spec
func calculateWaitTimeout(terminationGracePeriod *int64, containers []corev1.Container) int32 {
	var waitTimeout int32 = 0
	if terminationGracePeriod != nil {
		waitTimeout = int32(*terminationGracePeriod)
	}

	var containerMax int32 = 0
	for _, container := range containers {
		liveTimeout := calculateMaxProbeTimeout(container.LivenessProbe)
		if liveTimeout > containerMax {
			containerMax = liveTimeout
		}
		readyTimeout := calculateMaxProbeTimeout(container.ReadinessProbe)
		if readyTimeout > containerMax {
			containerMax = readyTimeout
		}
	}

	return waitTimeout + containerMax
}

// GetControllerStatus gets the status of a controller
func (c *Client) GetControllerStatus(ctx context.Context, namespace, controllerName, controllerType string) (*ControllerStatus, error) {
	if c.debug {
		fmt.Printf("Looking up status of %s for %s in %s\n", controllerType, controllerName, namespace)
	}

	status := &ControllerStatus{
		Want:        0,
		Ready:       0,
		Available:   0,
		WaitTimeout: 1,
	}

	switch controllerType {
	case "ReplicaSet":
		rs, err := c.clientset.AppsV1().ReplicaSets(namespace).Get(ctx, controllerName, metav1.GetOptions{})
		if err != nil {
			return nil, fmt.Errorf("failed to get ReplicaSet: %w", err)
		}
		if rs.Status.Replicas > 0 {
			status.Want = rs.Status.Replicas
		}
		if rs.Status.ReadyReplicas > 0 {
			status.Ready = rs.Status.ReadyReplicas
		}
		if rs.Status.AvailableReplicas > 0 {
			status.Available = rs.Status.AvailableReplicas
		}
		status.WaitTimeout = calculateWaitTimeout(
			rs.Spec.Template.Spec.TerminationGracePeriodSeconds,
			rs.Spec.Template.Spec.Containers,
		)

	case "StatefulSet":
		ss, err := c.clientset.AppsV1().StatefulSets(namespace).Get(ctx, controllerName, metav1.GetOptions{})
		if err != nil {
			return nil, fmt.Errorf("failed to get StatefulSet: %w", err)
		}
		if ss.Status.Replicas > 0 {
			status.Want = ss.Status.Replicas
		}
		if ss.Status.ReadyReplicas > 0 {
			status.Ready = ss.Status.ReadyReplicas
			status.Available = ss.Status.ReadyReplicas
		}
		status.WaitTimeout = calculateWaitTimeout(
			ss.Spec.Template.Spec.TerminationGracePeriodSeconds,
			ss.Spec.Template.Spec.Containers,
		)

	case "DaemonSet":
		ds, err := c.clientset.AppsV1().DaemonSets(namespace).Get(ctx, controllerName, metav1.GetOptions{})
		if err != nil {
			return nil, fmt.Errorf("failed to get DaemonSet: %w", err)
		}
		status.Want = ds.Status.DesiredNumberScheduled
		status.Ready = ds.Status.NumberReady
		status.Available = ds.Status.NumberAvailable
		status.WaitTimeout = calculateWaitTimeout(
			ds.Spec.Template.Spec.TerminationGracePeriodSeconds,
			ds.Spec.Template.Spec.Containers,
		)

	case "Job":
		fmt.Println("JOB type not yet supported")

	default:
		fmt.Printf("Unknown parent type: %s\n", controllerType)
	}

	return status, nil
}

// WaitForHealthyController waits for a controller to become healthy
func (c *Client) WaitForHealthyController(ctx context.Context, namespace, controllerName, controllerType string) (bool, error) {
	status, err := c.GetControllerStatus(ctx, namespace, controllerName, controllerType)
	if err != nil {
		return false, err
	}

	fmt.Printf("Current state of %s.%s in %s is want: %d, ready: %d, available: %d\n",
		controllerType, controllerName, namespace, status.Want, status.Ready, status.Available)

	waitTimeout := int(status.WaitTimeout) * PodDeleteMaxWait
	if c.debug {
		fmt.Printf("Waiting up to %d seconds for pod to stabilize\n", waitTimeout)
	}

	for i := 0; i < waitTimeout; i++ {
		status, err = c.GetControllerStatus(ctx, namespace, controllerName, controllerType)
		if err != nil {
			return false, err
		}
		if status.Want == status.Ready && status.Ready == status.Available {
			break
		}
		time.Sleep(1 * time.Second)
	}

	return status.Want == status.Ready && status.Ready == status.Available, nil
}

// DeletePod evicts a pod using the Eviction API
func (c *Client) DeletePod(ctx context.Context, namespace, podName string, gracePeriod int64) error {
	if gracePeriod == 0 {
		gracePeriod = 30
	}

	eviction := &policyv1.Eviction{
		ObjectMeta: metav1.ObjectMeta{
			Name:      podName,
			Namespace: namespace,
		},
		DeleteOptions: &metav1.DeleteOptions{
			GracePeriodSeconds: &gracePeriod,
		},
	}

	err := c.clientset.PolicyV1().Evictions(namespace).Evict(ctx, eviction)
	if err != nil {
		return fmt.Errorf("failed to evict pod: %w", err)
	}

	time.Sleep(time.Duration(gracePeriod+1) * time.Second)
	return nil
}

// SafeDeletePod safely deletes a pod after checking controller health
func (c *Client) SafeDeletePod(ctx context.Context, pod corev1.Pod) error {
	namespace := pod.Namespace
	podName := pod.Name

	if len(pod.OwnerReferences) == 0 {
		color.Yellow("*** %s is an orphan pod - that's weird and scary, so I'm outta here", podName)
		return nil
	}

	owner := pod.OwnerReferences[0]
	ownerType := owner.Kind
	ownerName := owner.Name

	if ownerType == "DaemonSet" {
		color.Yellow("*** %s is part of a daemonset, not deleting", podName)
		return nil
	}

	healthy, err := c.WaitForHealthyController(ctx, namespace, ownerName, ownerType)
	if err != nil {
		return err
	}
	if !healthy {
		yellow := color.New(color.FgYellow, color.BgRed)
		yellow.Printf("Timed out waiting for controller %s for %s to go healthy, not deleting\n", ownerType, podName)
		return nil
	}

	fmt.Printf("Service is healthy, deleting pod %s\n", podName)

	var gracePeriod int64 = 30
	if pod.Spec.TerminationGracePeriodSeconds != nil {
		gracePeriod = *pod.Spec.TerminationGracePeriodSeconds
	}

	if err := c.DeletePod(ctx, namespace, podName, gracePeriod); err != nil {
		return err
	}

	healthy, err = c.WaitForHealthyController(ctx, namespace, ownerName, ownerType)
	if err != nil {
		return err
	}
	if !healthy {
		yellow := color.New(color.FgYellow, color.BgRed)
		yellow.Printf("Timed out waiting for controller %s for %s to come back up healthy\n", ownerType, podName)
		return nil
	}

	if c.debug {
		fmt.Println("back to happy")
	}

	return nil
}

// SuffixedToNum converts K8s resource quantity string to numeric value
func SuffixedToNum(num string) int64 {
	if num == "" {
		return 0
	}

	// Binary suffixes (powers of 1024)
	binarySuffixes := map[string]int64{
		"Ki": 1024,
		"Mi": 1024 * 1024,
		"Gi": 1024 * 1024 * 1024,
		"Ti": 1024 * 1024 * 1024 * 1024,
		"Pi": 1024 * 1024 * 1024 * 1024 * 1024,
		"Ei": 1024 * 1024 * 1024 * 1024 * 1024 * 1024,
	}

	// Decimal suffixes (powers of 1000, plus fractional)
	decimalSuffixes := map[string]float64{
		"n": 1e-9,  // nano
		"u": 1e-6,  // micro
		"m": 1e-3,  // milli
		"k": 1e3,   // kilo
		"M": 1e6,   // mega
		"G": 1e9,   // giga
		"T": 1e12,  // tera
		"P": 1e15,  // peta
		"E": 1e18,  // exa
	}

	// Check for binary suffix (2 chars)
	if len(num) >= 2 {
		suffix := num[len(num)-2:]
		if mult, ok := binarySuffixes[suffix]; ok {
			val, _ := strconv.ParseInt(num[:len(num)-2], 10, 64)
			return val * mult
		}
	}

	// Check for decimal suffix (1 char)
	if len(num) >= 1 {
		suffix := string(num[len(num)-1])
		if mult, ok := decimalSuffixes[suffix]; ok {
			// Handle potential decimal values
			valStr := num[:len(num)-1]
			val, _ := strconv.ParseFloat(valStr, 64)
			return int64(val * mult)
		}
	}

	// Raw numeric value
	val, _ := strconv.ParseInt(strings.TrimSpace(num), 10, 64)
	return val
}

// GetMetrics retrieves pod metrics from the metrics API
func (c *Client) GetMetrics(ctx context.Context) (map[string]map[string]PodMetrics, error) {
	podMetricsList, err := c.metricsClient.MetricsV1beta1().PodMetricses("").List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("failed to get pod metrics: %w", err)
	}

	metrics := make(map[string]map[string]PodMetrics)

	for _, pm := range podMetricsList.Items {
		var cpu, mem int64
		for _, container := range pm.Containers {
			cpu += SuffixedToNum(container.Usage.Cpu().String())
			mem += SuffixedToNum(container.Usage.Memory().String())
		}

		namespace := pm.Namespace
		podName := pm.Name

		if _, ok := metrics[namespace]; !ok {
			metrics[namespace] = make(map[string]PodMetrics)
		}
		metrics[namespace][podName] = PodMetrics{CPU: cpu, Mem: mem}
	}

	return metrics, nil
}
