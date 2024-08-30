/*
 * Copyright (C) 2019-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#pragma once

#include "types/types.hh"
#include "utils/rjson.hh"

bytes from_json_object(const abstract_type &t, const rjson::value& value);
sstring to_json_string(const abstract_type &t, bytes_view bv, std::optional<bool> alternator);
sstring to_json_string(const abstract_type &t, const managed_bytes_view& bv, std::optional<bool> alternator);

inline sstring to_json_string(const abstract_type &t, const bytes& b, std::optional<bool> alternator = std::nullopt) {
    return to_json_string(t, bytes_view(b), alternator);
}

inline sstring to_json_string(const abstract_type& t, const bytes_opt& b, std::optional<bool> alternator = std::nullopt) {
    return b ? to_json_string(t, *b, alternator) : "null";
}
