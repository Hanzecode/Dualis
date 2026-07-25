// ─────────────────────────────────────────────────────────────────────────────
//  QUANTCORE — main.cpp  (v2 — with structured logging)
//
//  Startup sequence:
//  1. Logger::init()       — async spdlog, rotating file + console sinks
//  2. Build EngineConfig   — hardcoded defaults (replace with YAML in prod)
//  3. ExecutionEngine()    — creates BookRouters, binds ZMQ sockets
//  4. engine.start()       — launches ZMQ consumer + receiver threads
//  5. Block on SIGINT/SIGTERM — main thread prints status every 5s
//  6. engine.stop()        — joins threads, closes sockets
//  7. Logger::shutdown()   — flushes async queue, joins log thread
// ─────────────────────────────────────────────────────────────────────────────

#include "execution_engine.hpp"   // Pulls in all other headers
#include "logger.hpp"             // Structured logging

#include <csignal>      // std::signal, SIGINT, SIGTERM
#include <atomic>       // std::atomic<bool> — safe flag for signal handler
#include <thread>       // std::this_thread::sleep_for
#include <chrono>       // std::chrono::milliseconds
#include <sstream>      // std::ostringstream — for status string building
#include <string>
#include <vector>

// ─────────────────────────────────────────────────────────────────────────────
//  SIGNAL HANDLING
//  SIGINT (Ctrl-C) and SIGTERM (kill/docker stop) → set shutdown flag.
//  Signal handlers must be async-signal-safe:
//  - Only atomic stores and async-signal-safe C functions are permitted.
//  - No malloc, no mutexes, no spdlog (which uses a mutex internally).
// ─────────────────────────────────────────────────────────────────────────────

static std::atomic<bool> g_shutdown_requested{false};

void signal_handler(int /*signum*/) {
    g_shutdown_requested.store(true);
    // The main loop checks this flag every 500ms and calls engine.stop()
}

// ─────────────────────────────────────────────────────────────────────────────
//  BUILD CONFIG
//  In production: parse from a YAML file using yaml-cpp.
//    auto cfg = parse_yaml_config("config/engine.yaml");
//  For now: hardcoded values matching a local dev environment.
// ─────────────────────────────────────────────────────────────────────────────

EngineConfig build_config() {
    EngineConfig cfg;

    // ── Symbols ────────────────────────────────────────────────────────────
    // Extended to cover Wing B's full SECTOR_UNIVERSE so every signal Wing B
    // scores has a book to route into. Must match config/settings.py's
    // Settings.symbols on the Python side.
    cfg.symbols = {
        "AAPL", "MSFT", "TSLA", "NVDA", "GOOGL",   // Technology
        "JNJ", "PFE", "UNH", "ABBV",                // Healthcare
        "JPM", "BAC", "GS", "MS",                   // Financials
        "XOM", "CVX", "COP", "SLB",                 // Energy
    };

    // ── ZMQ endpoints ──────────────────────────────────────────────────────
    cfg.zmq_sub_endpoint  = "tcp://localhost:5555";  // SUB: alpha signals in
    cfg.zmq_pub_endpoint  = "tcp://*:5556";          // PUB: fills out
    cfg.zmq_pull_endpoint = "tcp://*:5557";          // PULL: orders in
    cfg.zmq_topic         = "signal";

    // ── Risk limits ────────────────────────────────────────────────────────
    cfg.risk_limits.max_position_per_symbol  = 10000;     // 10k shares max
    cfg.risk_limits.max_total_notional_bps   = 500000000; // $50k total notional
    cfg.risk_limits.max_order_size           = 1000;      // 1k shares per order
    cfg.risk_limits.max_orders_per_second    = 50;
    cfg.risk_limits.max_loss_bps             = 10000000;  // Halt at -$1k
    cfg.risk_limits.max_concentration        = 0.40;

    // ── PostgreSQL ─────────────────────────────────────────────────────────
    cfg.pg_connection_string =
        "host=localhost port=5434 dbname=quantcore user=quant password=quant";

    // ── Initial mid-prices for FlatOrderBook windows ───────────────────────
    // price_bps = price_dollars × 10000
    cfg.initial_prices_bps["AAPL"] = 1890000;   // $189.00
    cfg.initial_prices_bps["MSFT"] = 4150000;   // $415.00
    cfg.initial_prices_bps["TSLA"] = 2500000;   // $250.00
    cfg.initial_prices_bps["NVDA"] = 8750000;   // $875.00

    // Wing B universe additions — approximate seed prices for local dev only,
    // not live quotes. AlpacaFeed will correct these once quotes arrive.
    cfg.initial_prices_bps["GOOGL"] = 1750000;  // $175.00
    cfg.initial_prices_bps["JNJ"]   = 1550000;  // $155.00
    cfg.initial_prices_bps["PFE"]   =  280000;  // $28.00
    cfg.initial_prices_bps["UNH"]   = 5200000;  // $520.00
    cfg.initial_prices_bps["ABBV"]  = 1750000;  // $175.00
    cfg.initial_prices_bps["JPM"]   = 2100000;  // $210.00
    cfg.initial_prices_bps["BAC"]   =  400000;  // $40.00
    cfg.initial_prices_bps["GS"]    = 4700000;  // $470.00
    cfg.initial_prices_bps["MS"]    = 1050000;  // $105.00
    cfg.initial_prices_bps["XOM"]   = 1150000;  // $115.00
    cfg.initial_prices_bps["CVX"]   = 1600000;  // $160.00
    cfg.initial_prices_bps["COP"]   = 1100000;  // $110.00
    cfg.initial_prices_bps["SLB"]   =  450000;  // $45.00

    return cfg;
}

// ─────────────────────────────────────────────────────────────────────────────
//  STATUS LOGGER
//  Logs a snapshot of every book every N seconds via the structured logger.
//  In production this data also flows to the React dashboard via ZMQ PUB.
// ─────────────────────────────────────────────────────────────────────────────

void log_status(const ExecutionEngine& engine,
                const std::vector<std::string>& symbols) {
    for (const auto& sym : symbols) {
        const BookRouter* router = engine.get_router(sym);
        if (!router) {
            LOG_WARN("[STATUS] No router for symbol: " + sym);
            continue;
        }

        auto bid  = router->best_bid();
        auto ask  = router->best_ask();
        auto sprd = router->spread_bps();

        // Build a single structured log line per symbol
        std::ostringstream ss;
        ss << "[STATUS] sym=" << sym
           << " bid="   << (bid  ? "$" + std::to_string(*bid  / 10000.0) : "---")
           << " ask="   << (ask  ? "$" + std::to_string(*ask  / 10000.0) : "---")
           << " spread=" << (sprd ? std::to_string(*sprd) + "bps" : "---")
           << " bid_lvls=" << router->bid_levels()
           << " ask_lvls=" << router->ask_levels();

        LOG_INFO(ss.str());
    }

    // Risk summary
    const RiskManager& risk = engine.risk();
    int64_t pnl = risk.total_pnl_bps();

    std::ostringstream rs;
    rs << "[STATUS] pnl=$" << (pnl / 10000.0)
       << " halted=" << (risk.is_halted() ? "YES" : "no");
    LOG_INFO(rs.str());

    if (risk.is_halted()) {
        LOG_HALT("Daily loss limit breached — check positions immediately");
    }
}

// ─────────────────────────────────────────────────────────────────────────────
//  MAIN
// ─────────────────────────────────────────────────────────────────────────────

int main() {
    // ── 1. Logging ─────────────────────────────────────────────────────────
    // Initialise FIRST — so any errors during startup are captured.
    LoggerConfig log_cfg;
    log_cfg.log_dir     = "logs";
    log_cfg.level       = 2;      // INFO and above to console
    log_cfg.async       = true;   // Non-blocking ring buffer
    log_cfg.rotate_days = 30;

    Logger::instance().init(log_cfg);
    LOG_INFO("QuantCore starting — v2 dual book + PUSH/PULL");

    // ── 2. Signal handlers ─────────────────────────────────────────────────
    // Register BEFORE engine construction — if constructor throws, we still
    // want Ctrl-C to trigger clean teardown rather than hard abort.
    std::signal(SIGINT,  signal_handler);
    std::signal(SIGTERM, signal_handler);

    // ── 3. Config ──────────────────────────────────────────────────────────
    EngineConfig cfg = build_config();

    {
        // Log the config at startup for audit trail
        std::ostringstream ss;
        ss << "[CONFIG] symbols=";
        for (const auto& s : cfg.symbols) ss << s << " ";
        ss << "sub=" << cfg.zmq_sub_endpoint
           << " pub=" << cfg.zmq_pub_endpoint
           << " pull=" << cfg.zmq_pull_endpoint;
        LOG_INFO(ss.str());
    }

    // ── 4. Engine construction ─────────────────────────────────────────────
    // Wrapping in try/catch: if ZMQ bind fails (port in use), we log and exit
    // cleanly rather than crashing with an unhandled exception.
    // Copy symbols BEFORE moving cfg into the engine — cfg.symbols would be
    // moved-from (and therefore empty) if read anywhere after std::move(cfg).
    // This local copy is what the main-loop status logger uses below.
    const std::vector<std::string> symbols_for_status = cfg.symbols;

    std::unique_ptr<ExecutionEngine> engine;
    try {
        Logger::instance().log_engine_start(cfg.symbols);
        engine = std::make_unique<ExecutionEngine>(std::move(cfg));
    } catch (const std::exception& e) {
        LOG_CRITICAL(std::string("[STARTUP_FAIL] Engine construction failed: ") + e.what());
        Logger::instance().shutdown();
        return 1;   // Non-zero exit: systemd/docker will restart if configured
    }

    // ── 5. Start ───────────────────────────────────────────────────────────
    engine->start();
    LOG_INFO("[ENGINE] Running — listening on ZMQ sockets. Ctrl-C to stop.");

    // ── 6. Main loop ───────────────────────────────────────────────────────
    // Main thread: sleeps 500ms per iteration, logs status every 5s.
    // All real work happens on background threads (ZMQ consumer + receiver).
    constexpr int STATUS_INTERVAL_MS  = 5000;   // 5 seconds between status logs
    constexpr int SLEEP_INTERVAL_MS   = 500;    // Ctrl-C responsiveness
    int elapsed_ms = 0;

    while (!g_shutdown_requested.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(SLEEP_INTERVAL_MS));
        elapsed_ms += SLEEP_INTERVAL_MS;

        if (elapsed_ms >= STATUS_INTERVAL_MS) {
            log_status(*engine, symbols_for_status);   // Logs via structured logger
            elapsed_ms = 0;
        }
    }

    // ── 7. Graceful shutdown ───────────────────────────────────────────────
    LOG_INFO("[SHUTDOWN] Signal received — stopping engine...");

    try {
        engine->stop();    // Joins ZMQ threads, closes sockets, flushes PUB
    } catch (const std::exception& e) {
        LOG_ERR(std::string("[SHUTDOWN_ERR] ") + e.what());
    }

    Logger::instance().log_engine_stop();

    // ── 8. Logger shutdown ─────────────────────────────────────────────────
    // Must be LAST — flushes the async ring buffer so no log entries are lost.
    // After this, any LOG_* macro call is a no-op.
    Logger::instance().shutdown();

    return 0;   // Zero = success; systemd ExecStart= checks this
}
