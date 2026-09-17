Nodetool cluster freeze
=======================

**cluster freeze** - Freezes cluster-wide changes, so that the disks of all nodes can be snapshotted
(for example, with cloud disk snapshots) and later restored together as a working cluster.

**cluster unfreeze** - Unfreezes the cluster, or aborts a freeze which is in progress.

**cluster freeze-status** - Prints the freeze state of the cluster: ``none``, ``freezing`` or ``frozen``.

Running any of these commands on a **single node** applies to the whole cluster.

Freezing the cluster
--------------------

``nodetool cluster freeze`` waits for in-progress topology operations (such as node operations,
tablet migrations and tablet repairs) to finish, without starting new ones. Then, it rejects all
changes to cluster-wide state, which is managed by Raft: schema, topology, authentication, service
levels, and so on. Background operations which change this state, such as tablet load balancing,
are paused. Reads and writes of user data are not affected.

The command fails, and the cluster is left not frozen, if:

* any node is down,
* the freeze does not complete within ``--timeout`` seconds,
* ``nodetool cluster unfreeze`` is run before the freeze completes.

While the freeze is in progress, requests for new cluster-wide changes are rejected.

.. code-block:: shell

   nodetool cluster freeze [--timeout <seconds>]

======================  =================================================================================
Parameter               Description
======================  =================================================================================
``--timeout``           How long to wait for the cluster to become frozen, in seconds. ``0`` (the default)
                        means no timeout. Otherwise, it must be at least 300.
======================  =================================================================================

Once the command succeeds, take disk snapshots of all nodes. Snapshot all of a node's data
directories (including the commitlog and schema commitlog directories) together, so that the
snapshot of each node is crash-consistent.

The cluster stays frozen until ``nodetool cluster unfreeze`` is run. It remains frozen across node
restarts.

Unfreezing the cluster
----------------------

.. code-block:: shell

   nodetool cluster unfreeze

The command exits with a non-zero status if the freeze did not complete, or if changes were committed
to the Raft log while the cluster was frozen (for example, due to a Raft leader change, caused by
restarting or losing the leader node). In both cases, the disk snapshots taken while the cluster was
frozen are not guaranteed to be restorable together, and should be taken again.

Restoring a cluster
-------------------

A cluster restored from disk snapshots taken while it was frozen starts frozen. Either run
``nodetool cluster unfreeze`` once all nodes are up, or set the following option in ``scylla.yaml``
of any node before starting it:

.. code-block:: yaml

   unfreeze_cluster_on_startup: true

The node then unfreezes the cluster once enough nodes are up. The option has no effect if the cluster
is not frozen. Remove it after the cluster is restored, otherwise restarting the node unfreezes the
cluster, possibly while new disk snapshots are being taken.

The restored nodes may use different IP addresses than the original ones (update ``seeds`` in
``scylla.yaml`` accordingly), but must use the same cluster name, ScyllaDB version and number of shards.

Limitations
-----------

* Keyspaces with strongly consistent tables are not supported.
* All nodes must be upgraded to a version supporting cluster freeze.
