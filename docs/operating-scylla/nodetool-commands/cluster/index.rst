Nodetool cluster
================

.. toctree::
   :hidden:

   repair <repair>
   cleanup <cleanup>
   freeze <freeze>

**cluster** - Nodetool supercommand for running cluster operations.

Supported cluster suboperations
-------------------------------

* :doc:`repair </operating-scylla/nodetool-commands/cluster/repair>`  :code:`<keyspace>` :code:`<table>` - Repair one or more tablet tables.
* :doc:`cleanup </operating-scylla/nodetool-commands/cluster/cleanup>`  - Clean up all non tablet (vnode-based) keyspaces in a cluster
* :doc:`freeze </operating-scylla/nodetool-commands/cluster/freeze>`  - Freeze or unfreeze cluster-wide changes, to take disk snapshots of all nodes
