import random
import time

import kubernetes

from termcolor import colored

class NedryKube:
    _DEBUG = False

    # Wait up to 2x expected timeout for actions in pod deletion
    POD_DELETE_MAX_WAIT = 2

    def __init__(self):
        self._api = {}

    def k8s_ensure_initialized(self):
        if 'initialized' not in self._api:
            kubernetes.config.load_kube_config()
            self._api['initialized'] = True

    @property
    def api_core(self):
        if 'core' not in self._api:
            self.k8s_ensure_initialized()
            self._api['core'] = kubernetes.client.CoreV1Api()
            self._api['core'].pool = None
        return self._api['core']

    @property
    def api_apps(self):
        if 'apps' not in self._api:
            self.k8s_ensure_initialized()
            self._api['apps'] = kubernetes.client.AppsV1Api()
            self._api['apps'].pool = None
        return self._api['apps']

    @property
    def api_custom(self):
        if 'custom' not in self._api:
            self.k8s_ensure_initialized()
            self._api['custom'] = kubernetes.client.CustomObjectsApi()
            self._api['custom'].pool = None
        return self._api['custom']

    def get_worker_nodes(self):
        """Get worker nodes by excluding control-plane nodes.

        Modern K8s (1.24+) uses node-role.kubernetes.io/control-plane label.
        Worker nodes are identified by absence of control-plane role.
        """
        nodes = []
        node_list = self.api_core.list_node(watch=False)
        for n in node_list.items:
            labels = n.metadata.labels or {}
            # Worker nodes don't have the control-plane role label
            if 'node-role.kubernetes.io/control-plane' not in labels:
                nodes.append(n)
        return nodes

    def get_all_pods(self, ordered=False):
        ret = self.api_core.list_pod_for_all_namespaces(watch=False)
        if not ordered:
            random.shuffle(ret.items)
        return ret.items

    def get_pods_on_node(self, nodes):
        pods = []

        match_names = []
        for n in nodes:
            match_names.append(n.metadata.name)

        for p in self.get_all_pods():
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
            rs = self.api_apps.read_namespaced_replica_set_status(controller_name, namespace)
            controller_status['want'] = rs.status.replicas or 0
            controller_status['ready'] = rs.status.ready_replicas or 0
            controller_status['available'] = rs.status.available_replicas or 0
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
            ss = self.api_apps.read_namespaced_stateful_set_status(controller_name, namespace)
            controller_status['want'] = ss.status.replicas or 0
            controller_status['ready'] = ss.status.ready_replicas or 0
            controller_status['available'] = ss.status.ready_replicas or 0
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
            ds = self.api_apps.read_namespaced_daemon_set_status(controller_name, namespace)
            controller_status['want'] = ds.status.desired_number_scheduled or 0
            controller_status['ready'] = ds.status.number_ready or 0
            controller_status['available'] = ds.status.number_available or 0
            controller_status['wait_timeout'] = self.calculate_wait_timeout(ds.spec)

        elif controller_type == 'Job':
            print('JOB type not yet supported')

        else:
            print('Unknown parent type: {}'.format(controller_type))

        return controller_status

    def wait_for_healthy_controller(self, namespace, controller_name, controller_type):
        status = self.get_controller_status(namespace, controller_name, controller_type)
        print('Current state of {controller_type}.{controller_name} in {space} is '
              'want: {want}, ready: {ready}, available: {available}'.format(
                controller_type=controller_type,
                controller_name=controller_name,
                space=namespace,
                **status
                )
              )

        wait_timeout = status['wait_timeout'] * self.POD_DELETE_MAX_WAIT
        if self._DEBUG:
            print('Waiting up to {} seconds for pod to stabilize'.format(wait_timeout))

        for loop in range(wait_timeout):
            status = self.get_controller_status(namespace, controller_name, controller_type)
            if status['want'] == status['ready'] and status['ready'] == status['available']:
                break
            time.sleep(1)

        return status['want'] == status['ready'] and status['ready'] == status['available']

    def delete_pod(self, namespace, pod_name, grace_period):
        """Evict a pod using the Eviction API.

        Uses policy/v1 Eviction which respects PodDisruptionBudgets.
        This is the recommended way to remove pods in modern K8s (1.22+).
        """
        grace_period = grace_period if grace_period is not None else 30
        eviction = kubernetes.client.V1Eviction(
            metadata=kubernetes.client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace
            ),
            delete_options=kubernetes.client.V1DeleteOptions(
                grace_period_seconds=grace_period
            )
        )
        self.api_core.create_namespaced_pod_eviction(
            name=pod_name,
            namespace=namespace,
            body=eviction
        )
        time.sleep(grace_period + 1)

    def safe_delete_pod(self, pod):

        namespace = pod.metadata.namespace
        pod_name = pod.metadata.name

        if not pod.metadata.owner_references:
            print(colored("*** {} is an orphan pod - that's weird and scary, so I'm outta here".format(pod_name), 'yellow'))
            return

        owner = pod.metadata.owner_references[0]
        owner_type = owner.kind
        owner_name = owner.name

        if owner_type == 'DaemonSet':
            print(colored("*** {} is part of a daemonset, not deleting".format(pod_name), 'yellow'))
            return

        status = self.wait_for_healthy_controller(namespace, owner_name, owner_type)
        if status is False:
            print(colored('Timed out waiting for controller {owner_type} for {pod} to go healthy, not deleting'.format(
                owner_type=owner_type,
                pod=pod_name),
                'yellow',
                'on_red'
            ))
            return

        print('Service is healthy, deleting pod {}'.format(pod_name))

        self.delete_pod(namespace, pod_name, pod.spec.termination_grace_period_seconds)

        status = self.wait_for_healthy_controller(namespace, owner_name, owner_type)
        if status is False:
            print(colored('Timed out waiting for controller {owner_type} for {pod} to come back up healthy'.format(
                owner_type=owner_type,
                pod=pod_name),
                'yellow',
                'on_red'
            ))
            return

        if self._DEBUG:
            print('back to happy')

        return

    def suffixed_to_num(self, num):
        """Convert K8s resource quantity string to numeric value.

        Handles binary suffixes (Ki, Mi, Gi, Ti, Pi, Ei),
        decimal suffixes (n, u, m, k, M, G, T, P, E),
        and raw numeric values.
        """
        if not num:
            return 0

        # Binary suffixes (powers of 1024)
        binary_suffixes = {
            'Ki': 1024,
            'Mi': 1024 ** 2,
            'Gi': 1024 ** 3,
            'Ti': 1024 ** 4,
            'Pi': 1024 ** 5,
            'Ei': 1024 ** 6,
        }

        # Decimal suffixes (powers of 1000, plus fractional)
        decimal_suffixes = {
            'n': 1e-9,   # nano
            'u': 1e-6,   # micro
            'm': 1e-3,   # milli
            'k': 1e3,    # kilo
            'M': 1e6,    # mega
            'G': 1e9,    # giga
            'T': 1e12,   # tera
            'P': 1e15,   # peta
            'E': 1e18,   # exa
        }

        # Check for binary suffix (2 chars)
        if len(num) >= 2 and num[-2:] in binary_suffixes:
            return int(num[:-2]) * binary_suffixes[num[-2:]]

        # Check for decimal suffix (1 char)
        if num[-1] in decimal_suffixes:
            return int(float(num[:-1]) * decimal_suffixes[num[-1]])

        # Raw numeric value
        return int(num)

    def get_metrics(self):
        raw = self.api_custom.list_cluster_custom_object(
            group='metrics.k8s.io',
            version='v1beta1',
            plural='pods'
        )

        metrics = {}

        for e in raw['items']:
            cpu = 0
            mem = 0
            for c in e['containers']:
                usage = c['usage']
                cpu = cpu + self.suffixed_to_num(usage['cpu'])
                mem = mem + self.suffixed_to_num(usage['memory'])
            m = e['metadata']
            k8s_namespace = m['namespace']
            k8s_podname = m['name']
            if k8s_namespace not in metrics:
                metrics[k8s_namespace] = {}
            metrics[k8s_namespace][k8s_podname] = {'cpu': cpu, 'mem': mem}
        return metrics

