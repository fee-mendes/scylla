/*
 * Copyright (C) 2022-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#include "database_fwd.hh"
#include "compaction/compaction_descriptor.hh"
#include "sstables/sstable_set.hh"

#pragma once

namespace compaction {
class table_state;
}

namespace replica {

// Compaction group is a set of SSTables which are eligible to be compacted together.
// By this definition, we can say:
//      - A group contains SSTables that are owned by the same shard.
//      - Also, a group will be owned by a single table. Different tables own different groups.
//      - Each group can be thought of an isolated LSM tree, where Memtable(s) and SSTable(s) are
//          isolated from other groups.
// Usually, a table T in shard S will own a single compaction group. With compaction_group, a
// table T will be able to own as many groups as it wishes.
class compaction_group {
    table& _t;
    class table_state;
    std::unique_ptr<table_state> _table_state;
    // SSTable set which contains all non-maintenance sstables
    lw_shared_ptr<sstables::sstable_set> _main_sstables;
public:
    compaction_group(table& t);

    // Will stop ongoing compaction on behalf of this group, etc.
    future<> stop() noexcept;

    // Clear sstable sets
    void clear_sstables();

    // Add sstable to main set
    void add_sstable(sstables::shared_sstable sstable);

    // Update main sstable set based on info in completion descriptor, where input sstables
    // will be replaced by output ones, row cache ranges are possibly invalidated and
    // statistics are updated.
    future<> update_main_sstable_list_on_compaction_completion(sstables::compaction_completion_desc desc);

    const lw_shared_ptr<sstables::sstable_set>& main_sstables() const noexcept;
    void set_main_sstables(lw_shared_ptr<sstables::sstable_set> new_main_sstables);

    compaction::table_state& as_table_state() const noexcept;
};

}
