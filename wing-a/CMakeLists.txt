cmake_minimum_required(VERSION 3.20)    # 3.20+ for C++20 support and FetchContent improvements
project(QuantCore VERSION 0.1.0 LANGUAGES CXX)

# ── C++ standard ──────────────────────────────────────────────────────────────
# C++20 required for: designated initialisers ({.field = value}),
#                     std::jthread (if used), ranges, concepts
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)   # Fail if compiler can't do C++20
set(CMAKE_CXX_EXTENSIONS OFF)         # Use -std=c++20, not -std=gnu++20

# ── Compiler flags ────────────────────────────────────────────────────────────
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(
        -Wall -Wextra -Wpedantic      # Enable most warnings
        -Wshadow                       # Warn when a local variable shadows an outer one
        -Wnon-virtual-dtor             # Warn on base class without virtual destructor
        -Wno-unused-parameter          # Suppress during development (remove before prod)
    )
endif()

# Release build: -O3 enables vectorisation, SIMD, inlining; -DNDEBUG disables assert()
# Debug build:   -O0 -g includes debug symbols for gdb/lldb
# Toggle: cmake -DCMAKE_BUILD_TYPE=Release ..
#
# For FlatOrderBook specifically, Release mode matters:
# The ~4ns best_bid() claim assumes -O3 inlines price_to_slot() and
# eliminates the function call overhead. In Debug (-O0) it will be ~20-40ns.

# ── Dependencies (all optional — core library and tests build without them) ───
#
# WHY OPTIONAL?
# FlatOrderBook and BookRouter are header-only and depend only on the STL.
# OrderBook (std::map) implementation is in order_book.cpp — also STL-only.
# The three external deps (ZMQ, nlohmann, PostgreSQL) are only needed by:
#   - zmq_signal_consumer.hpp  -> needs cppzmq + nlohmann_json
#   - execution_engine.hpp     -> needs cppzmq + nlohmann_json + PostgreSQL
# So the library and all tests compile and run without any of them installed.

# ZeroMQ C++ bindings — needed by zmq_signal_consumer.hpp and execution_engine.hpp
find_package(cppzmq QUIET)               # QUIET = no error if not found; sets cppzmq_FOUND
if(cppzmq_FOUND)
    message(STATUS "cppzmq found — ZMQ transport enabled")
else()
    message(STATUS "cppzmq NOT found — ZMQ components will not be compiled")
endif()

# nlohmann/json — header-only JSON, needed by zmq_signal_consumer.hpp
find_package(nlohmann_json 3.2.0 QUIET)
if(nlohmann_json_FOUND)
    message(STATUS "nlohmann_json found")
else()
    message(STATUS "nlohmann_json NOT found — JSON parsing disabled")
endif()

# PostgreSQL client — needed by execution_engine.hpp persist_trade()
find_package(PostgreSQL QUIET)
if(PostgreSQL_FOUND)
    message(STATUS "PostgreSQL found")
else()
    message(STATUS "PostgreSQL NOT found — DB persistence disabled")
endif()

# ── Core library ──────────────────────────────────────────────────────────────
# What goes in here vs stays header-only:
#
#   order_book.cpp           -> compiled (non-trivial implementation, split for build speed)
#
#   flat_order_book.hpp      -> header-only (all inline; inlining is REQUIRED for ~4ns
#                               performance — compiler must see body at call sites)
#
#   book_router.hpp          -> header-only (thin orchestration layer; benefits from inlining)
#
#   risk_manager.hpp         -> header-only (inline implementations; could be split later)
#
#   zmq_signal_consumer.hpp  -> header-only (inline; ZMQ availability checked below)
#   execution_engine.hpp     -> header-only (inline; ZMQ + PG availability checked below)

add_library(quantcore_lib STATIC
    src/order_book.cpp          # OrderBook (std::map) — the only compiled translation unit
    # flat_order_book.hpp       <- header-only, no .cpp needed
    # book_router.hpp           <- header-only, no .cpp needed
    # risk_manager.hpp          <- header-only, no .cpp needed
)

# PUBLIC: any target that links quantcore_lib also gets the include/ directory
target_include_directories(quantcore_lib PUBLIC include)

# Conditionally link external deps only if found
if(cppzmq_FOUND)
    target_link_libraries(quantcore_lib PUBLIC cppzmq)
endif()

if(nlohmann_json_FOUND)
    target_link_libraries(quantcore_lib PUBLIC nlohmann_json::nlohmann_json)
endif()

if(PostgreSQL_FOUND)
    target_link_libraries(quantcore_lib PUBLIC PostgreSQL::PostgreSQL)
endif()

# ── Main executable ───────────────────────────────────────────────────────────
add_executable(quantcore src/main.cpp)
target_link_libraries(quantcore PRIVATE quantcore_lib)

# ── Tests ─────────────────────────────────────────────────────────────────────
enable_testing()   # Activates CTest — run with 'ctest --output-on-failure' after building

# ── Test suite 1: OrderBook (std::map) ───────────────────────────────────────
# Tests: submit, match, partial fill, cancel, FIFO priority, market sweep
add_executable(quantcore_tests tests/tests.cpp)
target_link_libraries(quantcore_tests PRIVATE quantcore_lib)
add_test(NAME OrderBookTests COMMAND quantcore_tests)

# ── Test suite 2: FlatOrderBook + BookRouter ─────────────────────────────────
# Tests: price->slot mapping, add/remove/set qty, best bid/ask cache,
#        depth query, recenter, BookRouter synchronisation
add_executable(flat_tests tests/flat_tests.cpp)
target_link_libraries(flat_tests PRIVATE quantcore_lib)
add_test(NAME FlatBookTests COMMAND flat_tests)

# ── How to build and test ─────────────────────────────────────────────────────
# mkdir build && cd build
# cmake ..                                    # configure (Debug by default)
# cmake .. -DCMAKE_BUILD_TYPE=Release         # configure Release (-O3)
# cmake --build .                             # compile everything
# ctest --output-on-failure                   # run all tests
# ctest -R FlatBookTests --output-on-failure  # run only flat book tests
