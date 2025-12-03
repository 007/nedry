package main

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/fatih/color"
	"github.com/spf13/cobra"

	"github.com/nedry/nedry/pkg/kube"
)

const (
	AnnotationPrefix    = "nedry-v2/"
	AnnotationAction    = AnnotationPrefix + "action"
	AnnotationSoftLimit = AnnotationPrefix + "limit"

	ActionDrain = "drain"
)

var kubeClient *kube.Client

func log(msg string) {
	fmt.Printf("%s: %s\n", time.Now().Format("2006-01-02T15:04:05.000000"), msg)
}

func initKubeClient() error {
	var err error
	kubeClient, err = kube.NewClient()
	if err != nil {
		return fmt.Errorf("failed to create Kubernetes client: %w", err)
	}
	return nil
}

func filterNodesByAction(ctx context.Context, action string) ([]string, error) {
	nodes, err := kubeClient.GetWorkerNodes(ctx)
	if err != nil {
		return nil, err
	}

	var filtered []string
	for _, node := range nodes {
		annotations := node.Annotations
		if annotations == nil {
			continue
		}
		if annotations[AnnotationAction] == action {
			filtered = append(filtered, node.Name)
		}
	}
	return filtered, nil
}

func nodesToDrain(ctx context.Context) ([]string, error) {
	nodes, err := kubeClient.GetWorkerNodes(ctx)
	if err != nil {
		return nil, err
	}

	var filtered []string
	for _, node := range nodes {
		annotations := node.Annotations
		if annotations == nil {
			continue
		}
		// Check if action is drain and node is unschedulable (cordoned)
		if annotations[AnnotationAction] == ActionDrain && node.Spec.Unschedulable {
			filtered = append(filtered, node.Name)
		}
	}
	return filtered, nil
}

func runDrain(cmd *cobra.Command, args []string) error {
	if err := initKubeClient(); err != nil {
		return err
	}
	ctx := context.Background()

	// Filter to actionable nodes
	actionableNodeNames, err := nodesToDrain(ctx)
	if err != nil {
		return fmt.Errorf("failed to get nodes to drain: %w", err)
	}

	if len(actionableNodeNames) == 0 {
		log("No nodes to drain")
		return nil
	}

	nodeNameSet := make(map[string]bool)
	for _, name := range actionableNodeNames {
		nodeNameSet[name] = true
	}

	pods, err := kubeClient.GetAllPods(ctx, false)
	if err != nil {
		return fmt.Errorf("failed to get pods: %w", err)
	}

	// Count pods to drain
	var podCount int
	for _, pod := range pods {
		if nodeNameSet[pod.Spec.NodeName] {
			podCount++
		}
	}

	log(fmt.Sprintf("Rescheduling %d pods", podCount))

	for _, p := range pods {
		if nodeNameSet[p.Spec.NodeName] {
			if err := kubeClient.SafeDeletePod(ctx, p); err != nil {
				fmt.Printf("Error deleting pod %s/%s: %v\n", p.Namespace, p.Name, err)
			}
		}
	}

	log("done")
	return nil
}

func runSoftLimit(cmd *cobra.Command, args []string) error {
	if err := initKubeClient(); err != nil {
		return err
	}
	ctx := context.Background()

	log("fetching pods")
	pods, err := kubeClient.GetAllPods(ctx, false)
	if err != nil {
		return fmt.Errorf("failed to get pods: %w", err)
	}

	log("fetching metrics")
	metrics, err := kubeClient.GetMetrics(ctx)
	if err != nil {
		return fmt.Errorf("failed to get metrics: %w", err)
	}

	log("mashing everything up")
	for _, pod := range pods {
		annotations := pod.Annotations
		if annotations == nil {
			continue
		}

		limitStr, ok := annotations[AnnotationSoftLimit]
		if !ok {
			continue
		}

		limit := kube.SuffixedToNum(limitStr)
		namespace := pod.Namespace
		podName := pod.Name

		nsMetrics, ok := metrics[namespace]
		if !ok {
			continue
		}

		podMetrics, ok := nsMetrics[podName]
		if !ok {
			continue
		}

		actual := podMetrics.Mem
		if actual > limit {
			yellow := color.New(color.FgYellow, color.BgRed)
			yellow.Printf("%s/%s: %d > %d, soft kill\n", namespace, podName, actual, limit)
			if err := kubeClient.SafeDeletePod(ctx, pod); err != nil {
				fmt.Printf("Error deleting pod %s/%s: %v\n", namespace, podName, err)
			}
		} else {
			green := color.New(color.FgGreen)
			green.Printf("%s/%s: %d < %d, no action\n", namespace, podName, actual, limit)
		}
	}

	return nil
}

func main() {
	rootCmd := &cobra.Command{
		Use:   "nedry",
		Short: "Nedry - small acts of chaos to reduce downtime",
		Long: `Nedry performs controlled chaos operations on Kubernetes clusters.

Annotate a node with 'nedry-v2/action=drain' and cordon it, then run 'nedry drain'
to do a safe, slow node drain.

Annotate a pod with 'nedry-v2/limit=400Mi', then run 'nedry softlimit' to perform
a graceful delete/restart instead of a hard OOM kill.`,
	}

	drainCmd := &cobra.Command{
		Use:   "drain",
		Short: "Drain a node safely",
		Long:  "Safely drain nodes that have been annotated with nedry-v2/action=drain and cordoned",
		RunE:  runDrain,
	}

	softlimitCmd := &cobra.Command{
		Use:   "softlimit",
		Short: "Run soft-kill for soft memory limits",
		Long:  "Check pods annotated with nedry-v2/limit and gracefully restart those exceeding their limit",
		RunE:  runSoftLimit,
	}

	rootCmd.AddCommand(drainCmd)
	rootCmd.AddCommand(softlimitCmd)

	if err := rootCmd.Execute(); err != nil {
		os.Exit(1)
	}
}
