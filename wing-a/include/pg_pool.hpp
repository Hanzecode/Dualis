#pragma once

// ─────────────────────────────────────────────────────────────────────────────
//  POSTGRESQL CONNECTION POOL
//
//  WHY A POOL?
//  Opening a pqxx::connection costs ~5ms (TCP handshake + PG auth).
//  A trade fires on_trade() potentially hundreds of times per second.
//  One connection per trade = 500ms of blocking per second = unusable.
//
//  A pool pre-opens N connections at startup and lends them out.
//  Acquire: wait if all busy (typically < 1ms), borrow a connection.
//  Release: return it automatically when the RAII guard goes out of scope.
//
//  THREAD SAFETY MODEL:
//  - acquire() blocks on a std::condition_variable until a connection is free.
//  - Multiple threads (trade callbacks from different symbols) can hold
//    different connections simultaneously.
//  - The pool itself is protected by std::mutex.
//  - Each pqxx::connection is owned by exactly one thread at a time.
//
//  USAGE:
//    PgPool pool("host=localhost dbname=quantcore user=quant", 4);
//
//    // Acquire a connection — RAII: returned when guard destructs
//    auto guard = pool.acquire();      // blocks if all 4 are busy
//    guard.conn().exec_params(
//        "INSERT INTO trades VALUES ($1, $2, $3)",
//        trade_id, symbol, price_bps
//    );
//    guard.conn().commit();            // commit the transaction
//    // guard destructs here → connection returned to pool
//
//  INSTALL:
//    apt install libpqxx-dev          (Ubuntu/Debian)
//    vcpkg install libpqxx            (Windows/vcpkg)
//    brew install libpqxx             (macOS)
//
//  ADD TO CMakeLists.txt:
//    find_package(libpqxx REQUIRED)
//    target_link_libraries(quantcore_lib PUBLIC libpqxx::pqxx)
// ─────────────────────────────────────────────────────────────────────────────

// Uncomment when libpqxx is installed:
// #include <pqxx/pqxx>

#include <vector>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <memory>
#include <string>
#include <stdexcept>
#include <chrono>
#include <functional>


// ─────────────────────────────────────────────────────────────────────────────
//  CONNECTION WRAPPER
//  Wraps a pqxx::connection and tracks whether it's healthy.
//  If a query throws a pqxx::broken_connection, we mark it unhealthy
//  so the pool can reconnect it before lending it out again.
// ─────────────────────────────────────────────────────────────────────────────

struct PgConnection {
    // pqxx::connection conn;       // Uncomment when libpqxx is available
    bool healthy = true;            // False if last operation threw broken_connection
    std::string connection_string;  // Stored so we can reconnect if needed

    // Construct — opens the connection immediately
    explicit PgConnection(const std::string& conn_str)
        : connection_string(conn_str)
    {
        // conn = pqxx::connection(conn_str);
        // If this throws pqxx::broken_connection, let it propagate — pool
        // constructor will fail loudly rather than silently using bad connections.
    }

    // Reconnect after a broken connection error
    void reconnect() {
        // conn = pqxx::connection(connection_string);
        healthy = true;
    }

    // Execute a parameterised INSERT — one-shot helper for trade persistence
    // Using exec_params prevents SQL injection even from internal callers
    template<typename... Args>
    void exec_params(const std::string& sql, Args&&... args) {
        // pqxx::work txn(conn);
        // txn.exec_params(sql, std::forward<Args>(args)...);
        // txn.commit();
        // RAII: pqxx::work auto-rollbacks if commit() is never called
        (void)sql;
        (void)(int[]){((void)args, 0)...};   // Suppress unused-parameter warnings
    }
};


// ─────────────────────────────────────────────────────────────────────────────
//  POOL GUARD — RAII connection lease
//  Returned by PgPool::acquire(). Holds the connection for the caller.
//  When it goes out of scope, the connection is automatically returned.
// ─────────────────────────────────────────────────────────────────────────────

class PgPool;   // Forward declaration — PgGuard needs to call pool.release()

class PgGuard {
public:
    // Non-copyable — only one owner at a time
    PgGuard(const PgGuard&)            = delete;
    PgGuard& operator=(const PgGuard&) = delete;

    // Movable — allows returning from acquire()
    PgGuard(PgGuard&&) noexcept            = default;
    PgGuard& operator=(PgGuard&&) noexcept = default;

    // Access the leased connection
    PgConnection& conn() {
        if (!conn_) throw std::runtime_error("PgGuard: no connection (moved-from guard)");
        return *conn_;
    }

    // Destructor — returns connection to pool
    ~PgGuard();   // Defined after PgPool

private:
    friend class PgPool;

    // Only PgPool can construct a guard
    PgGuard(PgPool* pool, std::unique_ptr<PgConnection> conn)
        : pool_(pool), conn_(std::move(conn)) {}

    PgPool*                       pool_;   // Raw pointer — pool outlives all guards
    std::unique_ptr<PgConnection> conn_;   // Owned while leased
};


// ─────────────────────────────────────────────────────────────────────────────
//  PG POOL CLASS
// ─────────────────────────────────────────────────────────────────────────────

class PgPool {
public:
    // Constructor — opens pool_size connections immediately.
    // Throws if any connection fails.
    // pool_size: 4 is a good default for a single-node engine.
    // More than the number of trade-callback threads is wasteful.
    PgPool(std::string connection_string, int pool_size = 4);

    // Destructor — waits for all leased connections to be returned,
    // then closes them. If called while connections are still in use,
    // it will block until they're all released (safe shutdown).
    ~PgPool();

    // Acquire a connection — blocks until one is available.
    // timeout: maximum wait time. Throws std::runtime_error if exceeded.
    // Typical wait: < 1ms if pool_size >= number of concurrent callers.
    [[nodiscard]] PgGuard acquire(
        std::chrono::milliseconds timeout = std::chrono::milliseconds(500));

    // Current number of idle (available) connections — for monitoring
    [[nodiscard]] int idle_count() const;

    // Total pool size
    [[nodiscard]] int pool_size() const { return pool_size_; }

    // Health check: attempt a SELECT 1 on all idle connections.
    // Call from a maintenance thread periodically (e.g. every 60s).
    void health_check();

private:
    friend class PgGuard;

    // Called by PgGuard destructor — returns the connection to the idle queue
    void release(std::unique_ptr<PgConnection> conn);

    std::string connection_string_;
    int         pool_size_;

    // Idle connections waiting to be leased
    // std::queue: FIFO — first returned is first lent out again
    std::queue<std::unique_ptr<PgConnection>> idle_;

    mutable std::mutex              mutex_;         // Protects idle_ queue
    std::condition_variable         cv_;            // Signals when a connection is returned
    bool                            shutting_down_; // Set in destructor
};


// ─────────────────────────────────────────────────────────────────────────────
//  IMPLEMENTATION
// ─────────────────────────────────────────────────────────────────────────────

inline PgPool::PgPool(std::string connection_string, int pool_size)
    : connection_string_(std::move(connection_string))
    , pool_size_(pool_size)
    , shutting_down_(false)
{
    if (pool_size <= 0) {
        throw std::invalid_argument("PgPool: pool_size must be > 0");
    }

    // Open all connections eagerly at startup.
    // Fail-fast: better to crash at startup than fail mid-trading.
    for (int i = 0; i < pool_size_; ++i) {
        // In production: replace with actual pqxx::connection construction
        idle_.push(std::make_unique<PgConnection>(connection_string_));
        // If any connection fails (DB not running, bad credentials),
        // PgConnection constructor throws and the pool construction fails.
    }
}

inline PgPool::~PgPool() {
    std::unique_lock lock(mutex_);

    shutting_down_ = true;

    // Wait until all leased connections have been returned
    // cv_.wait(lock, [this] { return static_cast<int>(idle_.size()) == pool_size_; });
    // Uncomment above when live; for now just drain the queue

    // Connections in idle_ will be destroyed when unique_ptrs are destroyed
    // pqxx::connection destructor closes the TCP connection cleanly
    while (!idle_.empty()) idle_.pop();
}

inline PgGuard PgPool::acquire(std::chrono::milliseconds timeout) {
    std::unique_lock lock(mutex_);

    // Wait for an idle connection — with a timeout
    bool got_one = cv_.wait_for(lock, timeout, [this] {
        return !idle_.empty() || shutting_down_;
    });

    if (shutting_down_) {
        throw std::runtime_error("PgPool::acquire: pool is shutting down");
    }
    if (!got_one) {
        // Timeout — all connections are busy; likely a pool size issue
        throw std::runtime_error(
            "PgPool::acquire: timed out waiting for connection after " +
            std::to_string(timeout.count()) + "ms — consider increasing pool_size"
        );
    }

    // Take the front connection from the idle queue
    auto conn = std::move(idle_.front());
    idle_.pop();

    // If the connection was marked unhealthy (broken pipe, server restart),
    // attempt a reconnect before handing it out
    if (!conn->healthy) {
        try {
            conn->reconnect();
        } catch (...) {
            // Reconnect failed — put back a fresh connection attempt
            // (or rethrow — your call depending on resilience requirements)
            conn->healthy = false;
            idle_.push(std::move(conn));   // Return broken conn (will retry next time)
            throw std::runtime_error("PgPool::acquire: connection unhealthy and reconnect failed");
        }
    }

    // Return a guard — RAII will call release() when guard destructs
    return PgGuard(this, std::move(conn));
    // PgGuard constructor is private — only PgPool can construct one
}

inline void PgPool::release(std::unique_ptr<PgConnection> conn) {
    {
        std::lock_guard lock(mutex_);   // lock_guard: simpler than unique_lock when
                                        // we don't need to unlock early or wait
        if (!shutting_down_) {
            idle_.push(std::move(conn));   // Return to idle queue
        }
        // If shutting_down_, just let the unique_ptr destruct — closes the connection
    }
    cv_.notify_one();   // Wake up one thread waiting in acquire()
    // notify_one, not notify_all: only one thread can use the returned connection
}

inline int PgPool::idle_count() const {
    std::lock_guard lock(mutex_);
    return static_cast<int>(idle_.size());
}

inline void PgPool::health_check() {
    std::lock_guard lock(mutex_);

    // Check each idle connection with a lightweight query
    std::queue<std::unique_ptr<PgConnection>> checked;
    while (!idle_.empty()) {
        auto conn = std::move(idle_.front());
        idle_.pop();

        try {
            // pqxx::work txn(conn->conn);
            // txn.exec("SELECT 1");   // Lightweight — just checks connectivity
            // txn.commit();
            conn->healthy = true;
        } catch (...) {
            // Connection is broken — try to reconnect
            try {
                conn->reconnect();
            } catch (...) {
                // Can't reconnect — mark unhealthy; acquire() will retry
                conn->healthy = false;
            }
        }
        checked.push(std::move(conn));
    }
    idle_ = std::move(checked);   // Put all (re)checked connections back
}

// ─────────────────────────────────────────────────────────────────────────────
//  PGGUARD DESTRUCTOR — defined here because it needs PgPool::release()
// ─────────────────────────────────────────────────────────────────────────────

inline PgGuard::~PgGuard() {
    if (conn_ && pool_) {
        // Return the connection to the pool — triggers notify_one() for waiters
        pool_->release(std::move(conn_));
    }
    // If conn_ is nullptr (moved-from guard), nothing to return
}


// ─────────────────────────────────────────────────────────────────────────────
//  TRADE PERSISTENCE HELPER
//  Standalone function — inject PgPool by reference, call from ExecutionEngine.
//  Keeps persistence logic out of the engine class.
// ─────────────────────────────────────────────────────────────────────────────

inline void persist_trade_to_pg(PgPool& pool,
                                 const Trade& trade,
                                 const std::string& symbol) {
    try {
        auto guard = pool.acquire();   // Blocks ≤ 500ms; throws if pool exhausted

        guard.conn().exec_params(
            R"(
                INSERT INTO trades
                    (trade_id, symbol, price_bps, quantity,
                     maker_order_id, taker_order_id, executed_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (trade_id) DO NOTHING
            )",
            // ON CONFLICT DO NOTHING: idempotent insert
            // Safe to retry on network hiccup without duplicating records
            static_cast<int64_t>(trade.trade_id),      // $1 — BIGINT in schema
            symbol,                                     // $2 — VARCHAR
            trade.price_bps,                           // $3 — BIGINT (bps)
            static_cast<int32_t>(trade.quantity),      // $4 — INTEGER
            static_cast<int64_t>(trade.maker_order_id), // $5 — BIGINT
            static_cast<int64_t>(trade.taker_order_id)  // $6 — BIGINT
        );
        // guard destructs here → connection returned to pool automatically

    } catch (const std::runtime_error& e) {
        // Pool timeout or connection failure — log and continue.
        // Trades are published via ZMQ PUB immediately; DB write is async
        // and can be replayed from the ZMQ stream if needed.
        // In production: use spdlog::error and push to a retry queue.
        // std::cerr << "[DB PERSIST ERROR] " << e.what() << "\n";
        (void)e;
    }
    // Note: pqxx::sql_error is not caught here — a SQL error (bad schema,
    // type mismatch) is a programming bug, not a runtime error.
    // Let it propagate to surface the bug early.
}
