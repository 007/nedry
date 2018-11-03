#!python

import operator
import random
import time

from cachetools import cachedmethod
from cachetools import TTLCache
import kubernetes


class Nedry:
    _DEBUG = False
    ANNOTATION_PREFIX = 'nedry-v1/'

    ANNOTATION_ACTION = ANNOTATION_PREFIX + 'action'
    ANNOTATION_SOFTLIMIT = ANNOTATION_PREFIX + 'limit'

    ACTION_NOMATCH = None
    ACTION_DRAIN = 'drain'

    # specified in seconds
    K8S_CACHE_TTL = 60
    K8S_CACHE_SIZE = 1024

    # Wait up to 2x expected timeout for actions in pod deletion
    POD_DELETE_MAX_WAIT = 2

    def __init__(self):
        kubernetes.config.load_kube_config()
        self.k8s_api_core = kubernetes.client.CoreV1Api()
        self.k8s_api_extv1b1 = kubernetes.client.ExtensionsV1beta1Api()
        self.k8s_api_appsv1b1 = kubernetes.client.AppsV1beta1Api()
        self.api_cache = TTLCache(self.K8S_CACHE_SIZE, self.K8S_CACHE_TTL)

    @cachedmethod(operator.attrgetter('api_cache'))
    def get_worker_nodes(self):
        nodes = []
        node_list = self.k8s_api_core.list_node(watch=False)
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
            if n.spec.unschedulable:
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

    def calculate_max_probe_timeout(self, probe):
        probe_timeout = probe.initial_delay_seconds
        probe_timeout += probe.success_threshold * (probe.timeout_seconds + probe.period_seconds)
        return probe_timeout

    def calculate_wait_timeout(self, spec):
        data = spec.template.spec
        wait_timeout = 0
        wait_timeout += data.termination_grace_period_seconds
        container_max = -1
        for container in data.containers:
            container_live_timeout = 0
            container_ready_timeout = 0
            if container.liveness_probe:
                container_live_timeout = self.calculate_max_probe_timeout(container.liveness_probe)
                if container_live_timeout > container_max:
                    container_max = container_live_timeout
            if container.readiness_probe:
                container_ready_timeout = self.calculate_max_probe_timeout(container.readiness_probe)
                if container_ready_timeout > container_max:
                    container_max = container_ready_timeout

        return wait_timeout + container_max

    def get_controller_status(self, namespace, controller_name, controller_type):
        if self._DEBUG:
            print('Looking up status of {controller_type} for {controller_name} in {space}'.format(
                controller_type=controller_type,
                controller_name=controller_name,
                space=namespace))

        controller_status = {'want': 0, 'ready': 0, 'available': 0, 'wait_timeout': 1}

        # from most-common to least-common within our cluster
        if controller_type == 'ReplicaSet':
            # {  # Ignore PyCommentedCodeBear
            #   "type": "ReplicaSet",
            #   "available_replicas": 1,
            #   "conditions": "",
            #   "fully_labeled_replicas": 1,
            #   "observed_generation": 3,
            #   "ready_replicas": 1,
            #   "replicas": 1
            # }
            rs = self.k8s_api_extv1b1.read_namespaced_replica_set_status(controller_name, namespace)
            controller_status['want'] = rs.status.replicas
            controller_status['ready'] = rs.status.ready_replicas
            controller_status['available'] = rs.status.available_replicas
            controller_status['wait_timeout'] = self.calculate_wait_timeout(rs.spec)

        elif controller_type == 'StatefulSet':
            # {  # Ignore PyCommentedCodeBear
            #   "type": "StatefulSet",
            #   "collision_count": "",
            #   "conditions": "",
            #   "current_replicas": "",
            #   "current_revision": "service-713823586",
            #   "observed_generation": 4,
            #   "ready_replicas": 3,
            #   "replicas": 3,
            #   "update_revision": "service-4122884199",
            #   "updated_replicas": 3
            # }
            ss = self.k8s_api_appsv1b1.read_namespaced_stateful_set_status(controller_name, namespace)
            controller_status['want'] = ss.status.replicas
            controller_status['ready'] = ss.status.ready_replicas
            controller_status['available'] = ss.status.ready_replicas
            controller_status['wait_timeout'] = self.calculate_wait_timeout(ss.spec)

        elif controller_type == 'DaemonSet':
            # {  # Ignore PyCommentedCodeBear
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
            ds = self.k8s_api_extv1b1.read_namespaced_daemon_set_status(controller_name, namespace)
            controller_status['want'] = ds.status.desired_number_scheduled
            controller_status['ready'] = ds.status.number_ready
            controller_status['available'] = ds.status.number_available
            controller_status['wait_timeout'] = self.calculate_wait_timeout(ds.spec)

        elif controller_type == 'Job':
            print('JOB type not yet supported')

        else:
            print('Unknown parent type: {}'.format(controller_type))

        return controller_status

    def wait_for_healthy_controller(self, namespace, controller_name, controller_type):
        status = self.get_controller_status(namespace, controller_name, controller_type)
        print('Current state of {controller_type}.{controller_name} in {space} is'
              'want: {want}, ready: {ready}, available: {available}'.format(
                controller_type=controller_type,
                controller_name=controller_name,
                space=namespace,
                **status
                )
              )

        wait_timeout = status['wait_timeout'] * self.POD_DELETE_MAX_WAIT
        print('Waiting up to {} seconds for pod to stabilize'.format(wait_timeout))

        for loop in range(wait_timeout):
            status = self.get_controller_status(namespace, controller_name, controller_type)
            if status['want'] == status['ready'] and status['ready'] == status['available']:
                break
            time.sleep(1)

        return status['want'] == status['ready'] and status['ready'] == status['available']

    def delete_pod(self, namespace, pod_name, grace_period):
        delete_options = kubernetes.client.V1DeleteOptions()
        response = self.k8s_api_core.delete_namespaced_pod(pod_name, namespace, delete_options)
        time.sleep(grace_period + 1)

    def safe_delete_pod(self, pod):

        namespace = pod.metadata.namespace
        pod_name = pod.metadata.name

        if pod.metadata.owner_references is None:
            print("*** {} is an orphan pod - that's weird and scary, so I'm outta here".format(pod_name))
            return

        owner = pod.metadata.owner_references[0]
        owner_type = owner.kind
        owner_name = owner.name

        status = self.wait_for_healthy_controller(namespace, owner_name, owner_type)
        if status is False:
            print('Timed out waiting for controller {owner_type} for {pod} to go healthy, not deleting'.format(
                owner_type=owner_type,
                pod=pod_name)
            )
            return

        print('Service is healthy, deleting pod {}'.format(pod_name))

        self.delete_pod(namespace, pod_name, pod.spec.termination_grace_period_seconds)

        status = self.wait_for_healthy_controller(namespace, owner_name, owner_type)
        if status is False:
            print('Timed out waiting for controller {owner_type} for {pod} to come back up healthy'.format(
                owner_type=owner_type,
                pod=pod_name)
            )
            return

        print('back to happy')
        return


nedry = Nedry()
actionable_nodes = nedry.nodes_to_drain()
pods_to_drain = nedry.get_pods_on_node(actionable_nodes)

print('Rescheduling {} pods'.format(len(pods_to_drain)))

random.shuffle(pods_to_drain)

for p in pods_to_drain:
    nedry.safe_delete_pod(p)

print('done')
