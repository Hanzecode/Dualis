#pragma once

// ─────────────────────────────────────────────────────────────────────────────
//  QUANTCORE LOGGER
//
//  WHY spdlog OVER std::cerr?
//  ┌─────────────────────┬──────────────────────┬────────────────────────┐
//  │                     │  std::cerr           │  spdlog                │
//  ├─────────────────────┼──────────────────────┼────────────────────────┤
//  │  Thread safety      │  ❌ interleaved lines │  ✅ atomic writes      │
//  │  Performance        │  ❌ blocking I/O      │  ✅ async ring buffer  │
//  │  Levels             │  ❌ none              │  ✅ trace/debug/info/  │
//  │                     │                      │     warn/error/crit    │
//  │  Timestamps         │  ❌ none              │  ✅ microsecond        │
//  │  Structured fields  │  ❌ string concat     │  ✅ fmt-style {}       │
//  │  Log rotation       │  ❌ none              │  ✅ daily/size-based   │
//  │  Flush on crash     │  ✅ (stderr always)   │  ✅ configurable       │
//  └─────────────────────┴──────────────────────┴────────────────────────┘
//
//  ARCHITECTURE — three sinks:
//
//  1. Rotating file sink  →  logs/quantcore_YYYY-MM-DD.log
//     All levels ≥ DEBUG. Rotates daily. Keeps 30 days.
//     Used for: post-trade audit, debugging, trade reconstruction.
//
//  2. Console sink (stdout) → coloured, human-readable
//     All levels ≥ INFO in production, DEBUG in dev.
//     Used for: live monitoring when no dashboard is open.
//
//  3. Async ring buffer between callers and sinks
//     Trade callbacks write to a lock-free queue; a background thread
//     drains to disk. Trade path never blocks on disk I/O.
//     Ring buffer size: 8192 log entries (configurable).
//
//  INSTALL:
//    apt install libspdlog-dev          (Ubuntu/Debian)
//    vcpkg install spdlog               (Windows/vcpkg)
//    brew install spdlog                (macOS)
//    FetchContent (CMake, no install needed — see CMakeLists.txt snippet below)
//
//  ADD TO CMakeLists.txt:
//    find_package(spdlog REQUIRED)
//    target_link_libraries(quantcore_lib PUBLIC spdlog::spdlog)
//
//  OR via FetchContent (no system install needed):
//    include(FetchContent)
//    FetchContent_Declare(spdlog
//        GIT_REPOSITORY https://github.com/gabime/spdlog.git
//        GIT_TAG        v1.14.1)
//    FetchContent_MakeAvailable(spdlog)
//    target_link_libraries(quantcore_lib PUBLIC spdlog::spdlog)
// ─────────────────────────────────────────────────────────────────────────────

// Uncomment when spdlog is installed:
// #include <spdlog/spdlog.h>
// #include <spdlog/async.h>                    // spdlog::init_thread_pool, async_logger
// #include <spdlog/sinks/rotating_file_sink.h> // Daily log rotation
// #include <spdlog/sinks/stdout_color_sinks.h> // Coloured console output
// #include <spdlog/fmt/ostr.h>                 // Support for operator<< types

#include <string>
#include <memory>
#include <filesystem>    // std::filesystem::create_directories — C++17
#include <iostream>      // Fallback when spdlog is not yet available
#include <sstream>
#include <chrono>
#include <iomanip>       // std::put_time — for timestamp in fallback


// ─────────────────────────────────────────────────────────────────────────────
//  LOGGER CONFIG
//  Passed to Logger::init() at startup — separates configuration from code.
// ─────────────────────────────────────────────────────────────────────────────

struct LoggerConfig {
    std::string log_dir        = "logs";      // Directory for log files
    std::string logger_name    = "quantcore"; // Name used in spdlog registry
    bool        console_output = true;        // Also log to stdout
    bool        async          = true;        // Use async ring buffer
    int         ring_buf_size  = 8192;        // Ring buffer capacity (log entries)
    int         rotate_days    = 30;          // Keep 30 days of log files

    // Minimum log level:
    // 0=trace, 1=debug, 2=info, 3=warn, 4=error, 5=critical
    // Production: 2 (info). Development: 1 (debug).
    int         level          = 2;
};


// ─────────────────────────────────────────────────────────────────────────────
//  LOG LEVEL ENUM
//  Mirrors spdlog levels — avoids exposing spdlog types to callers
//  before the library is available.
// ─────────────────────────────────────────────────────────────────────────────

enum class LogLevel {
    TRACE    = 0,
    DEBUG    = 1,
    INFO     = 2,
    WARN     = 3,
    ERR      = 4,   // ERROR is a Windows macro — use ERR instead
    CRITICAL = 5
};


// ─────────────────────────────────────────────────────────────────────────────
//  LOGGER CLASS
//  Singleton pattern — one logger instance per process, initialised once.
//  Singleton is justified here: logging is a cross-cutting concern that
//  genuinely should be global. Unlike most singletons it has no complex
//  dependencies and is thread-safe by design (spdlog's own thread pool).
// ─────────────────────────────────────────────────────────────────────────────

class Logger {
public:
    // ── Singleton access ──────────────────────────────────────────────────────
    // Returns the single Logger instance — created on first call (C++11 guarantees
    // static local variable initialisation is thread-safe)
    static Logger& instance() {
        static Logger inst;    // Constructed once, destroyed at program exit
        return inst;
    }

    // Delete copy/move — singletons must not be duplicated
    Logger(const Logger&)            = delete;
    Logger& operator=(const Logger&) = delete;
    Logger(Logger&&)                 = delete;
    Logger& operator=(Logger&&)      = delete;

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    // Call once at startup (from main.cpp, before engine construction).
    // Creates log directory, initialises spdlog thread pool, registers sinks.
    void init(const LoggerConfig& config = LoggerConfig{});

    // Flush all buffered log entries and shut down the async thread.
    // Call from main.cpp just before returning.
    void shutdown();

    // ── Logging methods ───────────────────────────────────────────────────────
    // Each method takes a pre-formatted string.
    // In production with spdlog available, use the LOG_* macros below instead
    // for zero-cost formatting when the level is disabled.

    void trace   (const std::string& msg) { log(LogLevel::TRACE,    msg); }
    void debug   (const std::string& msg) { log(LogLevel::DEBUG,    msg); }
    void info    (const std::string& msg) { log(LogLevel::INFO,     msg); }
    void warn    (const std::string& msg) { log(LogLevel::WARN,     msg); }
    void error   (const std::string& msg) { log(LogLevel::ERR,      msg); }
    void critical(const std::string& msg) { log(LogLevel::CRITICAL, msg); }

    // ── Structured helpers ────────────────────────────────────────────────────
    // Common log patterns in the engine — pre-formatted for consistency.

    void log_order_rejected(const std::string& symbol, int reason_code) {
        log(LogLevel::WARN,
            "[RISK_REJECT] symbol=" + symbol +
            " reason=" + std::to_string(reason_code));
    }

    void log_trade(uint64_t trade_id, const std::string& symbol,
                   int64_t price_bps, uint32_t qty) {
        std::ostringstream ss;
        ss << "[TRADE] id=" << trade_id
           << " symbol=" << symbol
           << " price=$" << (price_bps / 10000.0)
           << " qty=" << qty;
        log(LogLevel::INFO, ss.str());
    }

    void log_zmq_error(const std::string& component, const std::string& what) {
        log(LogLevel::ERR, "[ZMQ_ERR] " + component + ": " + what);
    }

    void log_db_error(const std::string& what) {
        log(LogLevel::ERR, "[DB_ERR] " + what);
    }

    void log_signal_received(const std::string& symbol, double signal, double confidence) {
        std::ostringstream ss;
        ss << "[SIGNAL] symbol=" << symbol
           << " signal=" << signal
           << " confidence=" << confidence;
        log(LogLevel::DEBUG, ss.str());
    }

    void log_halt(const std::string& reason) {
        log(LogLevel::CRITICAL, "[HALT] Trading halted — " + reason);
    }

    void log_engine_start(const std::vector<std::string>& symbols) {
        std::string sym_list;
        for (const auto& s : symbols) sym_list += s + " ";
        log(LogLevel::INFO, "[ENGINE_START] symbols=" + sym_list);
    }

    void log_engine_stop() {
        log(LogLevel::INFO, "[ENGINE_STOP] Graceful shutdown complete");
    }

private:
    Logger() = default;   // Private — only instance() can construct

    LoggerConfig config_;
    bool         initialised_ = false;

    // Core log dispatch — all public methods route through here
    void log(LogLevel level, const std::string& msg);

    // Fallback: when spdlog is not available, write to stderr with timestamp
    void fallback_log(LogLevel level, const std::string& msg);

    // Convert our LogLevel to a string prefix
    static const char* level_str(LogLevel level) {
        switch (level) {
            case LogLevel::TRACE:    return "TRC";
            case LogLevel::DEBUG:    return "DBG";
            case LogLevel::INFO:     return "INF";
            case LogLevel::WARN:     return "WRN";
            case LogLevel::ERR:      return "ERR";
            case LogLevel::CRITICAL: return "CRT";
        }
        return "???";
    }
};


// ─────────────────────────────────────────────────────────────────────────────
//  IMPLEMENTATION
// ─────────────────────────────────────────────────────────────────────────────

inline void Logger::init(const LoggerConfig& config) {
    config_      = config;
    initialised_ = true;

    // Create log directory if it doesn't exist
    // std::filesystem::create_directories: creates all parent dirs, no-op if exists
    std::filesystem::create_directories(config_.log_dir);

    // ── spdlog initialisation (uncomment when spdlog is available) ───────────
    //
    // if (config_.async) {
    //     // Initialise the global thread pool for async logging
    //     // ring_buf_size: number of log entries that can be queued before
    //     //                back-pressure. 8192 entries × ~200 bytes ≈ 1.6 MB.
    //     // 1 thread: one background thread drains the queue to sinks.
    //     //           More threads = more throughput but more CPU usage.
    //     spdlog::init_thread_pool(config_.ring_buf_size, 1);
    // }
    //
    // // ── Sinks ────────────────────────────────────────────────────────────
    // std::vector<spdlog::sink_ptr> sinks;
    //
    // // 1. Daily rotating file sink
    // // Rotates at midnight, keeps config_.rotate_days files
    // auto file_path = config_.log_dir + "/" + config_.logger_name + ".log";
    // auto file_sink = std::make_shared<spdlog::sinks::daily_file_sink_mt>(
    //     file_path,
    //     0,    // rotation hour (midnight)
    //     0     // rotation minute
    // );
    // // mt = multi-thread safe sink (st = single-thread, faster but unsafe)
    // file_sink->set_level(spdlog::level::debug);   // File captures everything DEBUG+
    // file_sink->set_pattern(
    //     "[%Y-%m-%d %H:%M:%S.%f] [%l] [%t] %v"
    //     // %Y-%m-%d  = date
    //     // %H:%M:%S  = time
    //     // %f        = microseconds
    //     // %l        = log level (INFO, WARN, etc.)
    //     // %t        = thread ID — critical for multi-threaded engine
    //     // %v        = the message
    // );
    // sinks.push_back(file_sink);
    //
    // // 2. Coloured console sink (optional)
    // if (config_.console_output) {
    //     auto console_sink = std::make_shared<spdlog::sinks::stdout_color_sink_mt>();
    //     console_sink->set_level(
    //         static_cast<spdlog::level::level_enum>(config_.level));
    //     console_sink->set_pattern("[%H:%M:%S.%f] [%^%l%$] %v");
    //     // %^...%$ = apply colour based on level (red for ERR, yellow for WARN)
    //     sinks.push_back(console_sink);
    // }
    //
    // // ── Create logger ─────────────────────────────────────────────────────
    // std::shared_ptr<spdlog::logger> logger;
    // if (config_.async) {
    //     logger = std::make_shared<spdlog::async_logger>(
    //         config_.logger_name,
    //         sinks.begin(), sinks.end(),
    //         spdlog::thread_pool(),           // Use the global thread pool
    //         spdlog::async_overflow_policy::block  // Block if ring buffer full
    //         // Alternative: ::overrun_oldest — drops oldest entry (never blocks)
    //         // For a trading engine: ::block is safer (no silent log loss)
    //     );
    // } else {
    //     logger = std::make_shared<spdlog::logger>(
    //         config_.logger_name, sinks.begin(), sinks.end());
    // }
    //
    // logger->set_level(spdlog::level::trace);   // Logger itself captures all levels
    //                                             // Individual sinks filter by their level
    //
    // // Flush on ERR and above — guarantees crash logs are written to disk
    // logger->flush_on(spdlog::level::err);
    //
    // // Register globally so any code can call spdlog::get("quantcore")
    // spdlog::register_logger(logger);
    //
    // // Set as the default logger for convenience (spdlog::info() works globally)
    // spdlog::set_default_logger(logger);
    // ── end spdlog init ───────────────────────────────────────────────────────

    fallback_log(LogLevel::INFO,
        "Logger initialised — log_dir=" + config_.log_dir +
        " level=" + std::to_string(config_.level) +
        " async=" + (config_.async ? "true" : "false") +
        " [spdlog not yet linked — using fallback]"
    );
}

inline void Logger::shutdown() {
    if (!initialised_) return;

    fallback_log(LogLevel::INFO, "Logger shutting down — flushing...");

    // spdlog::shutdown();   // Uncomment: flushes async queue, joins thread pool
    // All spdlog loggers are destroyed and deregistered.
    // After this call, any logging attempt will no-op.

    initialised_ = false;
}

inline void Logger::log(LogLevel level, const std::string& msg) {
    // When spdlog is available, replace the body with:
    //
    // auto logger = spdlog::get(config_.logger_name);
    // if (!logger) { fallback_log(level, msg); return; }
    //
    // switch (level) {
    //     case LogLevel::TRACE:    logger->trace(msg);    break;
    //     case LogLevel::DEBUG:    logger->debug(msg);    break;
    //     case LogLevel::INFO:     logger->info(msg);     break;
    //     case LogLevel::WARN:     logger->warn(msg);     break;
    //     case LogLevel::ERR:      logger->error(msg);    break;
    //     case LogLevel::CRITICAL: logger->critical(msg); break;
    // }

    // For now: fallback to timestamped stderr
    if (static_cast<int>(level) >= config_.level) {
        fallback_log(level, msg);
    }
}

inline void Logger::fallback_log(LogLevel level, const std::string& msg) {
    // Thread-safe: std::cerr writes are atomic for short strings on most platforms.
    // For the fallback, this is acceptable — spdlog replaces this entirely.

    // Build timestamp
    auto now = std::chrono::system_clock::now();
    auto t   = std::chrono::system_clock::to_time_t(now);
    auto ms  = std::chrono::duration_cast<std::chrono::milliseconds>(
                   now.time_since_epoch()) % 1000;

    std::ostringstream ss;
    ss << "[" << std::put_time(std::localtime(&t), "%H:%M:%S")
       << "." << std::setw(3) << std::setfill('0') << ms.count() << "]"
       << " [" << level_str(level) << "] "
       << msg << "\n";

    std::cerr << ss.str();   // Single write — less interleaving than multiple <<
}


// ─────────────────────────────────────────────────────────────────────────────
//  CONVENIENCE MACROS
//  Zero-cost when level is disabled — the condition is checked before
//  any string formatting happens, so fmt::format() is never called
//  for suppressed log levels.
//
//  Usage:
//    LOG_INFO("Order submitted id={} symbol={}", order_id, symbol);
//    LOG_WARN("Spread unusually wide: {}bps", spread);
//    LOG_ERR ("ZMQ recv failed: {}", e.what());
//
//  With spdlog available, replace Logger::instance().info(...) with
//  spdlog::info(...) for compile-time format string checking.
// ─────────────────────────────────────────────────────────────────────────────

// Simple macros that work without spdlog (string concat fallback)
// When spdlog is available, replace with: #define LOG_INFO(...) spdlog::info(__VA_ARGS__)

#define LOG_TRACE(msg)    Logger::instance().trace(msg)
#define LOG_DEBUG(msg)    Logger::instance().debug(msg)
#define LOG_INFO(msg)     Logger::instance().info(msg)
#define LOG_WARN(msg)     Logger::instance().warn(msg)
#define LOG_ERR(msg)      Logger::instance().error(msg)
#define LOG_CRITICAL(msg) Logger::instance().critical(msg)

// Structured helpers — commonly called from engine hot paths
#define LOG_TRADE(id, sym, price, qty) \
    Logger::instance().log_trade(id, sym, price, qty)

#define LOG_REJECT(sym, reason) \
    Logger::instance().log_order_rejected(sym, static_cast<int>(reason))

#define LOG_HALT(reason) \
    Logger::instance().log_halt(reason)

#define LOG_SIGNAL(sym, sig, conf) \
    Logger::instance().log_signal_received(sym, sig, conf)

#define LOG_ZMQ_ERR(component, what) \
    Logger::instance().log_zmq_error(component, what)

#define LOG_DB_ERR(what) \
    Logger::instance().log_db_error(what)
