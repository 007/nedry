#!python

import random

from kube import NedryKube

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

        random.shuffle(pods_to_drain)

        for p in pods_to_drain:
            self.kube.safe_delete_pod(p)

        print('done')

nedry = Nedry()
nedry.drain()
