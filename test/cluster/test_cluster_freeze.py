#
# Copyright (C) 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#

"""
Tests for cluster freeze (nodetool cluster freeze / unfreeze).

While the cluster is frozen, no changes are committed to the group 0 log, so that the
disks of all nodes can be snapshotted and restored together.
"""

import asyncio
import logging
import os
import pathlib
import shutil
import time

import pytest
from cassandra.protocol import InvalidRequest  # type: ignore

from test.pylib.rest_client import HTTPError, read_barrier
from test.pylib.scylla_cluster_manager import ScyllaClusterManager
from test.pylib.util import wait_for, wait_for_cql_and_get_hosts
from test.pylib.tablets import get_tablet_replica
from test.cluster.util import get_coordinator_host, new_test_keyspace

logger = logging.getLogger(__name__)

CMDLINE = [
    '--logger-log-level', 'raft_topology=debug',
    '--logger-log-level', 'group0_client=debug',
]


async def get_last_group0_state_id(manager: ScyllaClusterManager, server) -> str:
    cql = manager.get_cql()
    host = (await wait_for_cql_and_get_hosts(cql, [server], time.time() + 60))[0]
    await read_barrier(manager.api, server.ip_addr)
    rows = await cql.run_async("SELECT state_id FROM system.group0_history WHERE key = 'history' LIMIT 1", host=host)
    return str(rows[0].state_id)


async def wait_for_freeze_state(manager: ScyllaClusterManager, server, expected: str):
    async def check():
        return True if await manager.api.get_cluster_freeze_state(server.ip_addr) == expected else None
    await wait_for(check, time.time() + 60)


async def start_blocked_tablet_migration(manager: ScyllaClusterManager, servers, ks: str):
    """
    Starts migrating the only tablet of {ks}.t to another node, and blocks the migration in the
    streaming stage (until the block_tablet_streaming injection is messaged on all servers).
    Returns the task completing when the migration is done, and the source and destination servers.
    """
    cql = manager.get_cql()
    await cql.run_async(f"CREATE TABLE {ks}.t (pk int PRIMARY KEY, v int)")
    await asyncio.gather(*(cql.run_async(f"INSERT INTO {ks}.t (pk, v) VALUES ({i}, {i})") for i in range(100)))

    token = 0
    src_host, src_shard = await get_tablet_replica(manager, servers[0], ks, "t", token)
    host_ids = [await manager.get_host_id(s.server_id) for s in servers]
    src = next(s for s, h in zip(servers, host_ids) if h == src_host)
    dst = next(s for s, h in zip(servers, host_ids) if h != src_host)
    dst_host = host_ids[servers.index(dst)]

    await asyncio.gather(*(manager.api.enable_injection(s.ip_addr, "block_tablet_streaming", one_shot=False,
                                                        parameters={"keyspace": ks, "table": "t"}) for s in servers))
    log = await manager.server_open_log(dst.server_id)
    mark = await log.mark()
    move_task = asyncio.create_task(manager.api.move_tablet(servers[0].ip_addr, ks, "t", src_host, src_shard, dst_host, 0, token))
    await log.wait_for("block_tablet_streaming: waiting", from_mark=mark, timeout=60)
    return move_task, src, dst


async def unblock_tablet_migration(manager: ScyllaClusterManager, servers, move_task: asyncio.Task):
    await asyncio.gather(*(manager.api.message_injection(s.ip_addr, "block_tablet_streaming") for s in servers))
    await move_task


async def assert_freeze_state(manager: ScyllaClusterManager, servers, expected: str):
    for s in servers:
        await read_barrier(manager.api, s.ip_addr)
        state = await manager.api.get_cluster_freeze_state(s.ip_addr)
        assert state == expected, f"node {s.ip_addr}: expected freeze state {expected}, got {state}"


@pytest.mark.asyncio
async def test_cluster_freeze_and_unfreeze(manager: ScyllaClusterManager):
    """
    Freeze a cluster, check that group 0 changes are rejected while user data is still
    readable and writable, restart a node which is not the group 0 leader, and check that
    unfreezing reports the group 0 log did not advance.
    """
    servers = await manager.servers_add(3, cmdline=CMDLINE, auto_rack_dc="dc1")
    cql = manager.get_cql()

    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', "
                                 "'replication_factor': 3} AND tablets = {'initial': 4}") as ks:
        await cql.run_async(f"CREATE TABLE {ks}.t (pk int PRIMARY KEY, v int)")

        coordinator = await get_coordinator_host(manager)
        follower = next(s for s in servers if s.server_id != coordinator.server_id)

        await manager.api.cluster_freeze(follower.ip_addr)
        await assert_freeze_state(manager, servers, "frozen")

        # Freezing an already frozen cluster is a no-op.
        await manager.api.cluster_freeze(servers[0].ip_addr)

        state_id = await get_last_group0_state_id(manager, servers[0])

        with pytest.raises(InvalidRequest, match="Cluster is frozen"):
            await cql.run_async(f"CREATE TABLE {ks}.t2 (pk int PRIMARY KEY)")
        with pytest.raises(InvalidRequest, match="Cluster is frozen"):
            await cql.run_async(f"ALTER TABLE {ks}.t ADD v2 int")

        # User data is not affected.
        await cql.run_async(f"INSERT INTO {ks}.t (pk, v) VALUES (1, 1)")
        rows = await cql.run_async(f"SELECT v FROM {ks}.t WHERE pk = 1")
        assert rows[0].v == 1

        # Restarting a node which is not the leader must not change group 0.
        await manager.server_restart(follower.server_id)
        await manager.servers_see_each_other(servers)
        await assert_freeze_state(manager, servers, "frozen")
        assert await get_last_group0_state_id(manager, servers[0]) == state_id

        result = await manager.api.cluster_unfreeze(servers[0].ip_addr)
        assert result == {"previous_state": "frozen", "group0_log_advanced": False}
        await assert_freeze_state(manager, servers, "none")

        # Unfreezing a cluster which is not frozen is a no-op.
        result = await manager.api.cluster_unfreeze(servers[1].ip_addr)
        assert result == {"previous_state": "none", "group0_log_advanced": False}

        await cql.run_async(f"CREATE TABLE {ks}.t2 (pk int PRIMARY KEY)")


@pytest.mark.asyncio
async def test_cluster_unfreeze_detects_leader_change(manager: ScyllaClusterManager):
    """
    A group 0 leader change commits an entry to the group 0 log even while the cluster is
    frozen. Unfreezing must report it, since disk snapshots taken meanwhile may not be restorable.
    """
    servers = await manager.servers_add(3, cmdline=CMDLINE, auto_rack_dc="dc1")

    coordinator = await get_coordinator_host(manager)
    others = [s for s in servers if s.server_id != coordinator.server_id]

    await manager.api.cluster_freeze(others[0].ip_addr)
    await assert_freeze_state(manager, servers, "frozen")

    await manager.server_restart(coordinator.server_id)
    await manager.servers_see_each_other(servers)

    result = await manager.api.cluster_unfreeze(others[0].ip_addr)
    assert result == {"previous_state": "frozen", "group0_log_advanced": True}
    await assert_freeze_state(manager, servers, "none")


@pytest.mark.asyncio
async def test_cluster_freeze_fails_with_node_down(manager: ScyllaClusterManager):
    servers = await manager.servers_add(3, cmdline=CMDLINE, auto_rack_dc="dc1")

    await manager.server_stop_gracefully(servers[2].server_id)
    await manager.server_not_sees_other_server(servers[0].ip_addr, servers[2].ip_addr)

    with pytest.raises(HTTPError, match="nodes are down"):
        await manager.api.cluster_freeze(servers[0].ip_addr)
    await assert_freeze_state(manager, servers[:2], "none")

    # Group 0 changes are allowed after the failed freeze.
    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 1}"):
        pass


@pytest.mark.asyncio
async def test_cluster_freeze_invalid_timeout(manager: ScyllaClusterManager):
    servers = await manager.servers_add(1, cmdline=CMDLINE)

    with pytest.raises(HTTPError, match="at least 300 seconds"):
        await manager.api.cluster_freeze(servers[0].ip_addr, timeout=10)
    await assert_freeze_state(manager, servers, "none")


@pytest.mark.asyncio
async def test_cluster_unfreeze_on_startup(manager: ScyllaClusterManager):
    """
    Simulates bringing up a cluster restored from disk snapshots taken while it was frozen:
    all nodes are stopped while frozen and started again, one of them with unfreeze_cluster_on_startup.
    """
    servers = await manager.servers_add(3, cmdline=CMDLINE, auto_rack_dc="dc1")

    await manager.api.cluster_freeze(servers[0].ip_addr)
    await assert_freeze_state(manager, servers, "frozen")

    for s in servers:
        await manager.server_stop_gracefully(s.server_id)

    await manager.server_update_config(servers[0].server_id, "unfreeze_cluster_on_startup", True)
    await asyncio.gather(*(manager.server_start(s.server_id) for s in servers))
    await manager.servers_see_each_other(servers)

    async def unfrozen():
        for s in servers:
            if await manager.api.get_cluster_freeze_state(s.ip_addr) != "none":
                return None
        return True
    await wait_for(unfrozen, time.time() + 60)

    # All nodes were restarted, so reconnect the driver before running statements.
    await manager.driver_connect()
    cql = manager.get_cql()
    await wait_for_cql_and_get_hosts(cql, servers, time.time() + 60)
    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 3}") as ks:
        await cql.run_async(f"CREATE TABLE {ks}.t (pk int PRIMARY KEY)")


@pytest.mark.asyncio
@pytest.mark.skip_mode(mode='release', reason='error injections are not supported in release mode')
async def test_cluster_freeze_waits_for_tablet_migration(manager: ScyllaClusterManager):
    """
    A freeze requested while a tablet migration is in progress waits for it to finish. Meanwhile
    the cluster is freezing: user-initiated group 0 changes are rejected.
    """
    servers = await manager.servers_add(3, cmdline=CMDLINE)
    await manager.disable_tablet_balancing()
    cql = manager.get_cql()

    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', "
                                 "'replication_factor': 1} AND tablets = {'initial': 1}") as ks:
        move_task, _, _ = await start_blocked_tablet_migration(manager, servers, ks)

        freeze_task = asyncio.create_task(manager.api.cluster_freeze(servers[0].ip_addr))
        await wait_for_freeze_state(manager, servers[0], "freezing")

        with pytest.raises(InvalidRequest, match="Cluster is freezing"):
            await cql.run_async(f"CREATE TABLE {ks}.t2 (pk int PRIMARY KEY)")

        await asyncio.sleep(3)
        assert not freeze_task.done(), "the freeze completed while a tablet migration was in progress"

        await unblock_tablet_migration(manager, servers, move_task)
        await freeze_task
        await assert_freeze_state(manager, servers, "frozen")

        result = await manager.api.cluster_unfreeze(servers[1].ip_addr)
        assert result == {"previous_state": "frozen", "group0_log_advanced": False}
        await cql.run_async(f"CREATE TABLE {ks}.t2 (pk int PRIMARY KEY)")


@pytest.mark.asyncio
@pytest.mark.skip_mode(mode='release', reason='error injections are not supported in release mode')
async def test_cluster_unfreeze_aborts_freeze_in_progress(manager: ScyllaClusterManager):
    servers = await manager.servers_add(3, cmdline=CMDLINE)
    await manager.disable_tablet_balancing()
    cql = manager.get_cql()

    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', "
                                 "'replication_factor': 1} AND tablets = {'initial': 1}") as ks:
        move_task, _, _ = await start_blocked_tablet_migration(manager, servers, ks)

        freeze_task = asyncio.create_task(manager.api.cluster_freeze(servers[0].ip_addr))
        await wait_for_freeze_state(manager, servers[0], "freezing")

        result = await manager.api.cluster_unfreeze(servers[1].ip_addr)
        assert result == {"previous_state": "freezing", "group0_log_advanced": True}

        with pytest.raises(HTTPError, match="aborted"):
            await freeze_task
        await assert_freeze_state(manager, servers, "none")

        # Group 0 changes are allowed again, and the migration completes.
        await cql.run_async(f"CREATE TABLE {ks}.t2 (pk int PRIMARY KEY)")
        await unblock_tablet_migration(manager, servers, move_task)


@pytest.mark.asyncio
@pytest.mark.skip_mode(mode='release', reason='error injections are not supported in release mode')
async def test_cluster_freeze_aborts_when_node_dies_while_freezing(manager: ScyllaClusterManager):
    servers = await manager.servers_add(3, cmdline=CMDLINE)
    await manager.disable_tablet_balancing()
    cql = manager.get_cql()

    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', "
                                 "'replication_factor': 1} AND tablets = {'initial': 1}") as ks:
        move_task, src, dst = await start_blocked_tablet_migration(manager, servers, ks)

        # Stop a node which is not involved in the migration.
        victim = next(s for s in servers if s.server_id not in (src.server_id, dst.server_id))
        alive = [s for s in servers if s.server_id != victim.server_id]

        freeze_task = asyncio.create_task(manager.api.cluster_freeze(alive[0].ip_addr))
        await wait_for_freeze_state(manager, alive[0], "freezing")

        await manager.server_stop_gracefully(victim.server_id)

        with pytest.raises(HTTPError, match="nodes are down"):
            await freeze_task
        await assert_freeze_state(manager, alive, "none")

        # Group 0 changes are allowed again.
        await cql.run_async(f"CREATE TABLE {ks}.t2 (pk int PRIMARY KEY)")

        # The tablet migration can only complete once all nodes are up (it runs barriers on all of them).
        await manager.server_start(victim.server_id)
        await manager.servers_see_each_other(servers)
        await unblock_tablet_migration(manager, alive, move_task)


@pytest.mark.asyncio
@pytest.mark.skip_mode(mode='release', reason='error injections are not supported in release mode')
async def test_cluster_freeze_coordinator_restart_mid_freeze(manager: ScyllaClusterManager):
    """
    The topology coordinator is restarted after it moved the cluster to the frozen state, but before it
    completed the freeze. The cluster must not get stuck: either the new coordinator completes the freeze,
    or the node which requested it aborts it (since a node is down).
    """
    servers = await manager.servers_add(3, cmdline=CMDLINE, auto_rack_dc="dc1")

    coordinator = await get_coordinator_host(manager)
    others = [s for s in servers if s.server_id != coordinator.server_id]

    await manager.api.enable_injection(coordinator.ip_addr, "cluster_freeze_pause_before_completion", one_shot=False)
    log = await manager.server_open_log(coordinator.server_id)
    mark = await log.mark()

    freeze_task = asyncio.create_task(manager.api.cluster_freeze(others[0].ip_addr))
    await log.wait_for("cluster_freeze_pause_before_completion: waiting", from_mark=mark, timeout=60)
    await wait_for_freeze_state(manager, others[0], "frozen")

    await manager.server_restart(coordinator.server_id)
    await manager.servers_see_each_other(servers)

    try:
        await freeze_task
        expected = "frozen"
    except HTTPError as e:
        logger.info(f"freeze failed after the coordinator restart: {e}")
        expected = "none"

    await assert_freeze_state(manager, servers, expected)
    result = await manager.api.cluster_unfreeze(others[1].ip_addr)
    assert result["previous_state"] == expected
    await assert_freeze_state(manager, servers, "none")

    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 3}"):
        pass


@pytest.mark.asyncio
async def test_cluster_frozen_node_restart_with_different_shard_count(manager: ScyllaClusterManager):
    """
    A node whose shard count changed cannot update its metadata in the topology while the cluster
    is frozen, so it must refuse to start with a clear error.
    """
    servers = await manager.servers_add(3, cmdline=CMDLINE, auto_rack_dc="dc1")

    coordinator = await get_coordinator_host(manager)
    follower = next(s for s in servers if s.server_id != coordinator.server_id)

    await manager.api.cluster_freeze(servers[0].ip_addr)
    await assert_freeze_state(manager, servers, "frozen")

    await manager.server_stop_gracefully(follower.server_id)
    await manager.server_update_cmdline(follower.server_id, ["--smp", "3"])
    await manager.server_start(follower.server_id, expected_error="cannot be updated while the cluster is frozen")

    await manager.server_update_cmdline(follower.server_id, ["--smp", "2"])
    await manager.server_start(follower.server_id)
    await manager.servers_see_each_other(servers)
    await assert_freeze_state(manager, servers, "frozen")

    result = await manager.api.cluster_unfreeze(servers[0].ip_addr)
    assert result == {"previous_state": "frozen", "group0_log_advanced": False}


def copy_node_disk(workdir: pathlib.Path, dest: pathlib.Path):
    """Copies the node's disk, except for its configuration (which holds its IP addresses)."""
    def ignore(dir, names):
        ignored = [n for n in names if not (os.path.isdir(os.path.join(dir, n)) or os.path.isfile(os.path.join(dir, n)))]
        if pathlib.Path(dir) == workdir:
            ignored.append("conf")
        return ignored
    shutil.copytree(workdir, dest, symlinks=True, ignore=ignore)


def restore_node_disk(snapshot: pathlib.Path, workdir: pathlib.Path):
    for entry in workdir.iterdir():
        if entry.name != "conf":
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    for entry in snapshot.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, workdir / entry.name, symlinks=True)
        else:
            shutil.copy2(entry, workdir / entry.name, follow_symlinks=False)


@pytest.mark.asyncio
async def test_cluster_freeze_restore_from_disk_snapshots(manager: ScyllaClusterManager, tmp_path: pathlib.Path):
    """
    The use case of cluster freeze: take disk snapshots of all nodes while the cluster is frozen,
    and restore the whole cluster from them later.

    Each node's disk is copied while the node is paused (SIGSTOP), so the copy of each node is
    crash-consistent, but copies of different nodes are taken at different times - like cloud disk
    snapshots. The cluster keeps changing after it is unfrozen, then all nodes are stopped, restored
    from the copies and started with new IP addresses. The restored cluster must start, unfreeze,
    have the state from the time of the freeze, and be able to perform topology and schema changes.
    """
    servers = await manager.servers_add(3, cmdline=CMDLINE, auto_rack_dc="dc1")
    cql = manager.get_cql()

    ks = "ks_snapshot"
    await cql.run_async(f"CREATE KEYSPACE {ks} WITH replication = {{'class': 'NetworkTopologyStrategy', "
                        f"'replication_factor': 3}} AND tablets = {{'initial': 8}}")
    await cql.run_async(f"CREATE TABLE {ks}.t (pk int PRIMARY KEY, v int)")
    await asyncio.gather(*(cql.run_async(f"INSERT INTO {ks}.t (pk, v) VALUES ({i}, {i})") for i in range(1000)))

    # Keep changing group 0 in the background, before, during and after the freeze.
    stop_ddl = asyncio.Event()
    ddl_stats = {"ok": 0, "rejected": 0, "failed": 0}
    async def ddl_loop():
        i = 0
        while not stop_ddl.is_set():
            i += 1
            try:
                await cql.run_async(f"ALTER TABLE {ks}.t WITH comment = 'change {i}'")
                ddl_stats["ok"] += 1
            except InvalidRequest as e:
                assert "Cluster is fr" in str(e), f"unexpected error: {e}"
                ddl_stats["rejected"] += 1
            except Exception as e:
                # E.g. a timeout when the coordinator node is paused while its disk is copied.
                logger.info(f"background schema change failed: {e}")
                ddl_stats["failed"] += 1
            await asyncio.sleep(0.1)
    ddl_task = asyncio.create_task(ddl_loop())

    try:
        await asyncio.sleep(1)
        await manager.api.cluster_freeze(servers[0].ip_addr)
        await assert_freeze_state(manager, servers, "frozen")
        await asyncio.sleep(1)

        workdirs = [pathlib.Path(await manager.server_get_workdir(s.server_id)) for s in servers]
        snapshots = [tmp_path / f"node{s.server_id}" for s in servers]
        for s, workdir, snapshot in zip(servers, workdirs, snapshots):
            logger.info(f"copying the disk of {s} from {workdir} to {snapshot}")
            await manager.server_pause(s.server_id)
            try:
                await asyncio.to_thread(copy_node_disk, workdir, snapshot)
            finally:
                await manager.server_unpause(s.server_id)

        result = await manager.api.cluster_unfreeze(servers[1].ip_addr)
        assert result == {"previous_state": "frozen", "group0_log_advanced": False}

        await asyncio.sleep(1)
    finally:
        stop_ddl.set()
        await ddl_task
    logger.info(f"background schema changes: {ddl_stats}")
    assert ddl_stats["rejected"] > 0 and ddl_stats["ok"] > 0

    # Change the cluster after the snapshots were taken.
    await cql.run_async(f"CREATE TABLE {ks}.after_snapshot (pk int PRIMARY KEY)")
    await cql.run_async(f"INSERT INTO {ks}.t (pk, v) VALUES (100000, 100000)")
    await manager.api.quiesce_topology(servers[0].ip_addr)

    # Restore all nodes from the snapshots, with new IP addresses.
    for s in servers:
        await manager.server_stop_gracefully(s.server_id)
    new_ips = []
    for s, workdir, snapshot in zip(servers, workdirs, snapshots):
        await asyncio.to_thread(restore_node_disk, snapshot, workdir)
        new_ips.append(await manager.server_change_ip(s.server_id))
    logger.info(f"restored nodes with new IPs {new_ips}")

    await manager.server_update_config(servers[0].server_id, "unfreeze_cluster_on_startup", True)
    await asyncio.gather(*(manager.server_start(s.server_id, seeds=new_ips) for s in servers))

    servers = await manager.running_servers()
    await manager.driver_connect()
    cql = manager.get_cql()
    await manager.servers_see_each_other(servers)
    hosts = await wait_for_cql_and_get_hosts(cql, servers, time.time() + 60)

    async def unfrozen():
        for s in servers:
            if await manager.api.get_cluster_freeze_state(s.ip_addr) != "none":
                return None
        return True
    await wait_for(unfrozen, time.time() + 60)

    # The restored cluster has the state from the time of the freeze.
    rows = await cql.run_async(f"SELECT table_name FROM system_schema.tables WHERE keyspace_name = '{ks}'", host=hosts[0])
    assert {r.table_name for r in rows} == {"t"}
    for h in hosts:
        rows = await cql.run_async(f"SELECT pk FROM {ks}.t WHERE pk = 100000", host=h)
        assert not rows, f"row written after the snapshot is present on {h}"

    # And it is fully functional: schema changes, topology changes and tablet migrations work.
    await cql.run_async(f"CREATE TABLE {ks}.after_restore (pk int PRIMARY KEY)")
    # Adding a node makes the load balancer migrate tablets to it.
    await manager.server_add(cmdline=CMDLINE, property_file={"dc": "dc1", "rack": "rack1"})
    await manager.api.quiesce_topology(servers[0].ip_addr)
