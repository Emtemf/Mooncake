// Exercises the real ScopedBatchPutTrace / CurrentBatchPutTrace / suffix
// operator<< symbols from libmooncake_store (linked via add_store_test).
// Covers thread-local scoping, nesting, exception safety, id validation, the
// log suffix appearing only inside a scope, and cross-thread isolation.
//
// No glog initialization is needed: this test emits no log output. The trace
// symbols under test are pure C++ (thread_local state + stream operator), so
// they can be exercised without a Client, transfer engine, or RDMA device.
#include <gtest/gtest.h>

#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "batch_put_trace.h"

namespace mooncake::test {

TEST(BatchPutTraceTest, EmptyOutsideAnyScope) {
    EXPECT_TRUE(CurrentBatchPutTrace().empty());
}

TEST(BatchPutTraceTest, SuffixIsEmptyOutsideScope) {
    std::ostringstream out;
    out << "legacy" << BatchPutTraceLogSuffix{"BatchPut", "normal_full"};
    // Legacy log text must be byte-identical when no trace scope is active.
    EXPECT_EQ(out.str(), "legacy");
}

TEST(BatchPutTraceTest, ScopeSetsAndRestores) {
    {
        ScopedBatchPutTrace scope("trace-1");
        EXPECT_EQ(CurrentBatchPutTrace(), "trace-1");
    }
    EXPECT_TRUE(CurrentBatchPutTrace().empty());
}

TEST(BatchPutTraceTest, SuffixAppearsInsideScope) {
    ScopedBatchPutTrace scope("trace-1");
    std::ostringstream out;
    out << BatchPutTraceLogSuffix{"BatchPut", "normal_full"};
    EXPECT_EQ(out.str(),
              " trace_id=trace-1 path=BatchPut timing_scope=normal_full");
}

TEST(BatchPutTraceTest, PreferSameNodeScopesAreDistinguishable) {
    // phase1 and the remaining phases share one trace id but different scopes.
    ScopedBatchPutTrace scope("trace-1");
    std::ostringstream phase1;
    phase1 << BatchPutTraceLogSuffix{"BatchPut", "prefer_same_node_phase1"};
    std::ostringstream rest;
    rest << BatchPutTraceLogSuffix{"BatchPutWhenPreferSameNode",
                                   "prefer_same_node_remaining"};
    EXPECT_EQ(phase1.str(),
              " trace_id=trace-1 path=BatchPut"
              " timing_scope=prefer_same_node_phase1");
    EXPECT_EQ(rest.str(),
              " trace_id=trace-1 path=BatchPutWhenPreferSameNode"
              " timing_scope=prefer_same_node_remaining");
}

TEST(BatchPutTraceTest, NestingRestoresOuter) {
    ScopedBatchPutTrace outer("outer");
    {
        ScopedBatchPutTrace inner("inner");
        EXPECT_EQ(CurrentBatchPutTrace(), "inner");
    }
    EXPECT_EQ(CurrentBatchPutTrace(), "outer");
}

TEST(BatchPutTraceTest, ExceptionStillRestoresPrevious) {
    ScopedBatchPutTrace outer("outer");
    try {
        ScopedBatchPutTrace inner("inner");
        throw std::runtime_error("put failure");
    } catch (const std::runtime_error&) {
        // Propagated as the real traced wrapper does; scope unwinds.
    }
    EXPECT_EQ(CurrentBatchPutTrace(), "outer");
}

TEST(BatchPutTraceTest, RejectsInvalidIds) {
    ScopedBatchPutTrace outer("outer");
    // Every entry violates the documented rule: 1-256 printable ASCII chars
    // (0x21-0x7e), no whitespace.
    const std::vector<std::string> invalid = {
        "",                      // empty
        "has space",             // space (0x20)
        "has\ttab",              // real tab (0x09)
        "has\nnewline",          // newline (0x0a)
        std::string(257, 'a'),   // too long (> 256)
        std::string(1, '\x7f'),  // DEL, above the printable range
        std::string(1, '\x20'),  // space, below the printable range
    };
    for (const auto& id : invalid) {
        bool rejected = false;
        try {
            ScopedBatchPutTrace bad(id);
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        EXPECT_TRUE(rejected) << "expected rejection for an invalid trace id";
        // A rejected construction must not disturb the active scope.
        EXPECT_EQ(CurrentBatchPutTrace(), "outer");
    }
}

TEST(BatchPutTraceTest, AcceptsPrintableBoundaries) {
    ScopedBatchPutTrace backslash("a\\b");  // 0x5c is printable, must pass
    EXPECT_EQ(CurrentBatchPutTrace(), "a\\b");
    {
        ScopedBatchPutTrace low(std::string(1, '\x21'));  // '!'
        EXPECT_EQ(CurrentBatchPutTrace(), "!");
    }
    {
        ScopedBatchPutTrace high(std::string(1, '\x7e'));  // '~'
        EXPECT_EQ(CurrentBatchPutTrace(), "~");
    }
    {
        ScopedBatchPutTrace max_len(std::string(256, 'a'));  // exactly 256
        EXPECT_EQ(CurrentBatchPutTrace().size(), 256u);
    }
    EXPECT_EQ(CurrentBatchPutTrace(), "a\\b");
}

TEST(BatchPutTraceTest, ChildThreadDoesNotInherit) {
    ScopedBatchPutTrace outer("outer");
    int child_saw = -1;
    std::thread worker([&] {
        child_saw = CurrentBatchPutTrace().empty() ? 0 : 1;
        // A scope local to the child does not leak back to the parent.
        ScopedBatchPutTrace local("thread-local");
        EXPECT_EQ(CurrentBatchPutTrace(), "thread-local");
    });
    worker.join();
    EXPECT_EQ(child_saw, 0);
    EXPECT_EQ(CurrentBatchPutTrace(), "outer");
}

}  // namespace mooncake::test
