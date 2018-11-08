Nedry - small acts of chaos to reduce downtime

Annotate a node with `nedry-v1/action=drain` and cordon it, then run `nedry.drain` to do a safe, slow node drain.

Annotate a pod with `nedry-v1/limit=400Mi`, then run `nedry.softlimit` to perform a graceful delete/restart instead of a hard OOM kill.
