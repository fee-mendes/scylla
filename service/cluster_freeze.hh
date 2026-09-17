/*
 * Copyright (C) 2026-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
 */

#pragma once

#include <cstdint>
#include <fmt/format.h>
#include <seastar/core/sstring.hh>

#include "exceptions/exceptions.hh"

namespace service {

// Cluster freeze stops all changes to group 0 so that the disks of all nodes can be
// snapshotted (e.g. cloud disk snapshots) and later restored as a consistent set.
//
// none     -> the cluster is not frozen.
// freezing -> a freeze was requested. Group 0 changes initiated by users are rejected,
//             the topology coordinator finishes the work that is already in progress
//             (no new work is started), then moves the state to `frozen`.
// frozen   -> no group 0 changes are allowed, except for unfreezing the cluster.
//
// The state is stored in system.topology, so it survives coordinator changes and restarts.
enum class cluster_freeze_state : uint8_t {
    none,
    freezing,
    frozen,
};

seastar::sstring cluster_freeze_state_to_string(cluster_freeze_state);
cluster_freeze_state cluster_freeze_state_from_string(const seastar::sstring&);

// Which group 0 changes an operation is allowed to commit, given the current freeze state.
enum class cluster_freeze_policy : uint8_t {
    // The default for all operations. Rejected while the cluster is freezing or frozen.
    reject_when_freezing,
    // Work that has to finish before the cluster can become frozen
    // (e.g. the topology coordinator advancing in-progress transitions).
    allow_when_freezing,
    // Only for freezing and unfreezing the cluster itself.
    allow_when_frozen,
};

// Thrown when a group 0 change is attempted while the cluster is freezing or frozen.
class cluster_frozen_exception : public exceptions::invalid_request_exception {
public:
    explicit cluster_frozen_exception(cluster_freeze_state state);
};

} // namespace service

template <> struct fmt::formatter<service::cluster_freeze_state> : fmt::formatter<string_view> {
    auto format(service::cluster_freeze_state s, fmt::format_context& ctx) const -> decltype(ctx.out()) {
        return fmt::format_to(ctx.out(), "{}", service::cluster_freeze_state_to_string(s));
    }
};
