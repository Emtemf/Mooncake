#pragma once

#include <ostream>
#include <string_view>

namespace mooncake {

// Borrowed ID; caller owns the string until this synchronous scope ends.
// Storage lives in the core library, shared by bindings and Client.
class ScopedBatchPutTrace {
   public:
    explicit ScopedBatchPutTrace(std::string_view trace_id);
    ~ScopedBatchPutTrace() noexcept;
    ScopedBatchPutTrace(const ScopedBatchPutTrace&) = delete;
    ScopedBatchPutTrace& operator=(const ScopedBatchPutTrace&) = delete;

   private:
    std::string_view previous_;
};

std::string_view CurrentBatchPutTrace() noexcept;

struct BatchPutTraceLogSuffix {
    std::string_view path;
    std::string_view timing_scope;
};

// Empty outside a traced call; legacy log text stays unchanged.
std::ostream& operator<<(std::ostream& out, BatchPutTraceLogSuffix suffix);

}  // namespace mooncake
