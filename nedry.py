#!python

from kube import NedryKube
from termcolor import cprint


class Nedry:
    _DEBUG = False
    ANNOTATION_PREFIX = 'nedry-v1/'

    ANNOTATION_ACTION = ANNOTATION_PREFIX + 'action'
    ANNOTATION_SOFTLIMIT = ANNOTATION_PREFIX + 'limit'

    ACTION_NOMATCH = None
    ACTION_DRAIN = 'drain'

    def __init__(self):
        self.kube = NedryKube()

    def filter_nodes_by_action(self, action=ACTION_NOMATCH):
        filtered = []
        for n in self.kube.get_worker_nodes():
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

    def drain(self):
        actionable_nodes = self.nodes_to_drain()
        pods_to_drain = self.kube.get_pods_on_node(actionable_nodes)

        print('Rescheduling {} pods'.format(len(pods_to_drain)))

        for p in pods_to_drain:
            self.kube.safe_delete_pod(p)

        print('done')

    def softlimit(self):
        print("fetching pods")
        pods = self.kube.get_all_pods()
        print("fetching metrics")
        metrics = self.kube.get_metrics()
        print("mashing everything up")
        for p in pods:
            if self.ANNOTATION_SOFTLIMIT in p.metadata.annotations:
                limit = self.kube.suffixed_to_num(p.metadata.annotations[self.ANNOTATION_SOFTLIMIT])
                k8s_namespace = p.metadata.namespace
                k8s_podname = p.metadata.name
                # print('got one! {}/{}'.format(k8s_namespace, k8s_podname))
                if k8s_namespace in metrics:
                    ns_metrics = metrics[k8s_namespace]
                    if k8s_podname in ns_metrics:
                        actual = ns_metrics[k8s_podname]['mem']
                        if actual > limit:
                            cprint('{ns}/{pod}: {actual} > {limit}, soft kill'.format(
                                    actual=actual,
                                    limit=limit,
                                    ns=k8s_namespace,
                                    pod=k8s_podname),
                                'yellow',
                                'on_red'
                                )
                        else:
                            cprint('{ns}/{pod}: {actual} < {limit}, no action'.format(
                                    actual=actual,
                                    limit=limit,
                                    ns=k8s_namespace,
                                    pod=k8s_podname),
                                'green'
                                )


nedry = Nedry()
# nedry.drain()
nedry.softlimit()
