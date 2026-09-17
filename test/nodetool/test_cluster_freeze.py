#
# Copyright 2026-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#

from test.nodetool.rest_api_mock import expected_request


def test_cluster_freeze(nodetool, scylla_only):
    res = nodetool("cluster", "freeze", expected_requests=[
        expected_request("POST", "/storage_service/cluster_freeze"),
    ])
    assert res.stdout == "The cluster is frozen\n"


def test_cluster_freeze_timeout(nodetool, scylla_only):
    nodetool("cluster", "freeze", "--timeout", "600", expected_requests=[
        expected_request("POST", "/storage_service/cluster_freeze", params={"timeout": "600"}),
    ])


def test_cluster_unfreeze(nodetool, scylla_only):
    res = nodetool("cluster", "unfreeze", expected_requests=[
        expected_request("POST", "/storage_service/cluster_unfreeze",
                         response={"previous_state": "frozen", "group0_log_advanced": False}),
    ])
    assert res.stdout == "The cluster is unfrozen (it was frozen)\n"


def test_cluster_unfreeze_not_frozen(nodetool, scylla_only):
    res = nodetool("cluster", "unfreeze", expected_requests=[
        expected_request("POST", "/storage_service/cluster_unfreeze",
                         response={"previous_state": "none", "group0_log_advanced": False}),
    ])
    assert res.stdout == "The cluster was not frozen\n"


def test_cluster_unfreeze_log_advanced(nodetool, scylla_only):
    res = nodetool("cluster", "unfreeze", expected_requests=[
        expected_request("POST", "/storage_service/cluster_unfreeze",
                         response={"previous_state": "frozen", "group0_log_advanced": True}),
    ], check_return_code=False)
    assert res.returncode == 1
    assert res.stdout == "The cluster is unfrozen (it was frozen)\n"
    assert "not guaranteed to be restorable" in res.stderr


def test_cluster_freeze_status(nodetool, scylla_only):
    res = nodetool("cluster", "freeze-status", expected_requests=[
        expected_request("GET", "/storage_service/cluster_freeze", response="frozen"),
    ])
    assert res.stdout == "frozen\n"
