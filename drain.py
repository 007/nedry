#!python

import kubernetes
import operator
import json

#from kubernetes import client, config

from cachetools import cachedmethod, TTLCache


class Nedry:
    ANNOTATION_PREFIX = 'nedry-v1/'

    ANNOTATION_ACTION = ANNOTATION_PREFIX + 'action'
    ANNOTATION_SOFTLIMIT = ANNOTATION_PREFIX + 'limit'

    ACTION_NOMATCH = None
    ACTION_DRAIN = 'drain'

    # specified in seconds
    K8S_CACHE_TTL = 60
    K8S_CACHE_SIZE = 1024

    def __init__(self):
        kubernetes.config.load_kube_config()
        self.k8s_api_core = kubernetes.client.CoreV1Api()
        self.k8s_api_extv1b1 = kubernetes.client.ExtensionsV1beta1Api()
        self.k8s_api_appsv1b1 = kubernetes.client.AppsV1beta1Api()
        self.api_cache = TTLCache(self.K8S_CACHE_SIZE, self.K8S_CACHE_TTL)

    @cachedmethod(operator.attrgetter('api_cache'))
    def get_worker_nodes(self):
        nodes = []
        node_list = self.k8s_api_core.list_node()
        for n in node_list.items:
            if 'kubernetes.io/role' in n.metadata.labels:
                if n.metadata.labels['kubernetes.io/role'] == 'node':
                    nodes.append(n)
        return nodes

    def filter_nodes_by_action(self, action=ACTION_NOMATCH):
        filtered = []
        for n in self.get_worker_nodes():
            # skip node if it has no annotation
            if self.ANNOTATION_ACTION not in n.metadata.annotations:
                continue
            # attempt to match our filter
            if n.metadata.annotations[self.ANNOTATION_ACTION] == action:
                filtered.append(n)
        return filtered

    def nodes_to_drain(self):
        filtered = []
        for n in self.filter_nodes_by_action(self.ACTION_DRAIN):
#            if n.spec.unschedulable:
                filtered.append(n)
        return filtered

    def get_pods_on_node(self, nodes):
        pods = []

        match_names = []
        for n in nodes:
            match_names.append(n.metadata.name)

        ret = self.k8s_api_core.list_pod_for_all_namespaces(watch=False)
        for p in ret.items:
            if p.spec.node_name in match_names:
                pods.append(p)
        return pods

    def get_owner_status(self, namespace, owner_name, owner_type):
        print('Looking up status of {owner_type} for {owner_name} in {space}'.format(owner_type=owner_type, owner_name=owner_name, space=namespace))

        owner_status = {'desired':0, 'ready':0, 'available':0}

        # from most-common to least-common within our cluster
        if owner_type == "ReplicaSet":
            # {
            #   "type": "ReplicaSet",
            #   "available_replicas": 1,
            #   "conditions": "",
            #   "fully_labeled_replicas": 1,
            #   "observed_generation": 3,
            #   "ready_replicas": 1,
            #   "replicas": 1
            # }
            rs = self.k8s_api_extv1b1.read_namespaced_replica_set_status(owner_name, namespace)
            owner_status['desired'] = rs.status.replicas
            owner_status['ready'] = rs.status.ready_replicas
            owner_status['available'] = rs.status.available_replicas

        elif owner_type == "StatefulSet":
            # {
            #   "type": "StatefulSet",
            #   "collision_count": "",
            #   "conditions": "",
            #   "current_replicas": "",
            #   "current_revision": "rabbitmq-713823586",
            #   "observed_generation": 4,
            #   "ready_replicas": 3,
            #   "replicas": 3,
            #   "update_revision": "rabbitmq-4122884199",
            #   "updated_replicas": 3
            # }
            ss = self.k8s_api_appsv1b1.read_namespaced_stateful_set_status(owner_name, namespace)
            owner_status['desired'] = ss.status.replicas
            owner_status['ready'] = ss.status.ready_replicas
            owner_status['available'] = ss.status.ready_replicas

        elif owner_type == 'DaemonSet':
            # {
            #   "type": "DaemonSet",
            #   "collision_count": "",
            #   "conditions": "",
            #   "current_number_scheduled": 3,
            #   "desired_number_scheduled": 3,
            #   "number_available": 3,
            #   "number_misscheduled": 0,
            #   "number_ready": 3,
            #   "number_unavailable": "",
            #   "observed_generation": 32,
            #   "updated_number_scheduled": 3
            # }
            ds = self.k8s_api_extv1b1.read_namespaced_daemon_set_status(owner_name, namespace)
            owner_status['desired'] = ds.status.desired_number_scheduled
            owner_status['ready'] = ds.status.number_ready
            owner_status['available'] = ds.status.number_available

        elif owner_type == 'Job':
            print('JOB type not yet supported')

        else:
            print('Unknown parent type: {}'.format(owner_type))

        return owner_status


    def safe_delete_pod(self, pod):

        pod_name = pod.metadata.name
        namespace = pod.metadata.namespace
        print("checking current state of pod {pod} in {space}".format(pod=pod_name,space=namespace))

        if pod.metadata.owner_references is None:
            print("*** {} is an orphan pod? that's weird and scary".format(pod_name))
            return

        owner = pod.metadata.owner_references[0]
        owner_type = owner.kind
        owner_name = owner.name

        status = self.get_owner_status(namespace, owner_name, owner_type)

        print("Want: {desired}, have {ready} with {available} finished initialization".format(**status))

nedry = Nedry()
actionable_nodes = nedry.nodes_to_drain()
pods_to_drain = nedry.get_pods_on_node(actionable_nodes)

#p = pods_to_drain[0]
#nedry.safe_delete_pod(p)


for p in pods_to_drain:
    nedry.safe_delete_pod(p)

print("done")

