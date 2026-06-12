"""Kubernetes adapters. Importing this package registers them with the AdapterRegistry."""

from apex.adapters.k8s.cluster_inventory import KubernetesClusterInventoryAdapter

__all__ = ["KubernetesClusterInventoryAdapter"]
