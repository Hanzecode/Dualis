#pragma once

// ─────────────────────────────────────────────────────────────────────────────
//  EXECUTION ENGINE  (v2 — dual book + PUSH/PULL)
//
//  Full data flow matching your architecture diagram:
//
//  Python order manager
//       │ PUSH/PULL tcp:5557
//       ▼
//  PushPullReceiver ──► on_push_message()
//
//  Python alpha engine
//       │ PUB/SUB tcp:5555
//       ▼
//  ZmqSignalConsumer ──► on_signal()
//
//  Both paths ▼
//  RiskManager::check()         pre-trade gate
//       ▼
//  BookRouter::submit()
//  ┌──────────────────────┬────────────────────────────┐
//  │  FlatOrderBook       │  OrderBook (std::map)      │
//  │  price discovery     │  lifecycle, cancel, fills  │
//  └──────────────────────┴────────────────────────────┘
//       ▼ on_trade callback
//  RiskManager::on_trade()      post-trade position + PnL
//       ▼
//  ZMQ PUB tcp:5556 ──► React dashboard + Python PnL
//       ▼
//  PostgreSQL trades table
// ─────────────────────────────────────────────────────────────────────────────

#include "book_router.hpp"
#include "push_pull_receiver.hpp"
#include "risk_manager.hpp"
#include "zmq_signal_consumer.hpp"

#include "logger.hpp"
#include <chrono>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

// ─────────────────────────────────────────────────────────────────────────────
//  ENGINE CONFIG
// ─────────────────────────────────────────────────────────────────────────────

struct EngineConfig {
  std::vector<std::string> symbols;
  std::string zmq_sub_endpoint;  // PUB/SUB signals in:  "tcp://localhost:5555"
  std::string zmq_pub_endpoint;  // PUB fills out:       "tcp://*:5556"
  std::string zmq_pull_endpoint; // PUSH/PULL orders in: "tcp://*:5557"
  std::string zmq_topic;         // PUB/SUB filter:      "signal"
  RiskLimits risk_limits;
  std::string
      pg_connection_string; // "host=localhost dbname=quantcore user=quant"

  // Starting mid-price per symbol for FlatOrderBook window initialisation
  // (bps). If missing for a symbol, FlatOrderBook centres at 10000 bps and
  // recenters on first order.
  std::unordered_map<std::string, int64_t> initial_prices_bps;
};

// ─────────────────────────────────────────────────────────────────────────────
//  EXECUTION ENGINE CLASS
// ─────────────────────────────────────────────────────────────────────────────

class ExecutionEngine {
public:
  explicit ExecutionEngine(EngineConfig config);
  ~ExecutionEngine() noexcept;

  void start();
  void stop() noexcept;

  // Manual order entry — used by tests and admin CLI
  uint64_t submit_order(Order order);

  // Cancel a resting order
  bool cancel_order(const std::string &symbol, uint64_t order_id);

  // Market data update → FlatOrderBook only
  void on_market_depth(const std::string &symbol, Side side, int64_t price_bps,
                       uint32_t qty);

  [[nodiscard]] const BookRouter *get_router(const std::string &symbol) const;
  [[nodiscard]] const RiskManager &risk() const { return risk_; }
  [[nodiscard]] bool running() const { return running_; }

private:
  EngineConfig config_;
  bool running_ = false;

  // One BookRouter per symbol — BookRouter owns FlatOrderBook + OrderBook
  mutable std::shared_mutex routers_mutex_;
  std::unordered_map<std::string, std::unique_ptr<BookRouter>> routers_;
  // Note: pending_taker_sides_ has been removed.
  // taker_side is now carried directly in Trade::taker_side (set in
  // execute_fill). This eliminates the UINT64_MAX sentinel data race under
  // concurrent submissions.

  RiskManager risk_;

  // ZMQ: PUB socket for broadcasting fills
  zmq::context_t pub_context_;
  zmq::socket_t pub_socket_;
  std::mutex pub_mutex_; // pub_socket_ not thread-safe; serialise sends

  // ZMQ: PUB/SUB consumer (alpha signals from Python)
  ZmqSignalConsumer signal_consumer_;

  // ZMQ: PUSH/PULL receiver (order manager from Python)
  PushPullReceiver order_receiver_;

  // ── Private methods ───────────────────────────────────────────────────────
  void create_router(const std::string &symbol);

  // Single internal submit path — all public submission routes go through here.
  // taker_side must be supplied by caller (known before order is moved).
  uint64_t submit_internal(Order order, Side taker_side);

  void on_signal(const AlphaSignal &signal);
  void on_push_message(IncomingMessage msg);
  void on_trade(const Trade &trade,
                const std::string &symbol); // taker_side in trade.taker_side
  void publish_fill(const Trade &trade, const std::string &symbol);
  void persist_trade(const Trade &trade, const std::string &symbol);

  [[nodiscard]] std::string trade_to_json(const Trade &trade,
                                          const std::string &symbol) const;
};

// ─────────────────────────────────────────────────────────────────────────────
//  IMPLEMENTATION
// ─────────────────────────────────────────────────────────────────────────────

inline ExecutionEngine::ExecutionEngine(EngineConfig config)
    : config_(std::move(config)), risk_(config_.risk_limits), pub_context_(1),
      pub_socket_(pub_context_, zmq::socket_type::pub),
      signal_consumer_(config_.zmq_sub_endpoint, config_.zmq_topic,
                       [this](const AlphaSignal &s) { this->on_signal(s); }),
      order_receiver_(config_.zmq_pull_endpoint, [this](IncomingMessage m) {
        this->on_push_message(std::move(m));
      }) {
  for (const auto &sym : config_.symbols) {
    create_router(sym);
  }
  pub_socket_.bind(config_.zmq_pub_endpoint);
}

inline ExecutionEngine::~ExecutionEngine() noexcept { stop(); }

inline void ExecutionEngine::create_router(const std::string &symbol) {
  int64_t base_price = 10000;
  auto it = config_.initial_prices_bps.find(symbol);
  if (it != config_.initial_prices_bps.end())
    base_price = it->second;

  // Trade callback: taker_side now comes directly from trade.taker_side
  // (set in OrderBook::execute_fill which has the taker Order in hand).
  // The old pending_taker_sides_ sentinel map has been removed — it had a
  // data race under concurrent submissions.
  auto trade_cb = [this, symbol](const Trade &trade) {
    this->on_trade(trade, symbol); // taker_side read from trade.taker_side
  };

  std::unique_lock lock(routers_mutex_);
  routers_.emplace(symbol, std::make_unique<BookRouter>(symbol, base_price,
                                                        std::move(trade_cb)));
}

inline void ExecutionEngine::start() {
  signal_consumer_.start();
  order_receiver_.start();
  running_ = true;
}

inline void ExecutionEngine::stop() noexcept {
  signal_consumer_.stop();
  order_receiver_.stop();
  pub_socket_.set(zmq::sockopt::linger, 0);
  pub_socket_.close();
  running_ = false;
}

// ─────────────────────────────────────────────────────────────────────────────
//  SUBMIT_INTERNAL
//  Single funnel for ALL order submissions — signal path, PUSH/PULL path,
//  and manual submit_order(). taker_side must be resolved BEFORE calling.
// ─────────────────────────────────────────────────────────────────────────────

inline uint64_t ExecutionEngine::submit_internal(Order order, Side taker_side) {
  // Pre-trade risk check
  auto check = risk_.check(order);
  if (!check.approved()) {
    LOG_REJECT(order.symbol, check.reason);
    throw std::runtime_error("Order rejected: reason=" +
                             std::to_string(static_cast<int>(check.reason)));
  }

  std::shared_lock lock(routers_mutex_);
  auto it = routers_.find(order.symbol);
  if (it == routers_.end()) {
    throw std::invalid_argument("No book for symbol: " + order.symbol);
  }

  // Submit to BookRouter — fires trade callback synchronously for market
  // orders. taker_side is now embedded in Trade::taker_side by execute_fill(),
  // so we no longer need the pending_taker_sides_ sentinel map. The taker_side
  // parameter here is still used for signal/push-pull path clarity but is no
  // longer needed for the callback — Trade.taker_side is the source of truth.
  return it->second->submit(std::move(order));
}

inline uint64_t ExecutionEngine::submit_order(Order order) {
  Side taker_side = order.side; // Capture before move
  return submit_internal(std::move(order), taker_side);
}

inline bool ExecutionEngine::cancel_order(const std::string &symbol,
                                          uint64_t order_id) {
  std::shared_lock lock(routers_mutex_);
  auto it = routers_.find(symbol);
  if (it == routers_.end())
    return false;
  return it->second->cancel(order_id);
}

inline void ExecutionEngine::on_market_depth(const std::string &symbol,
                                             Side side, int64_t price_bps,
                                             uint32_t qty) {
  std::shared_lock lock(routers_mutex_);
  auto it = routers_.find(symbol);
  if (it == routers_.end())
    return;
  it->second->on_market_depth(side, price_bps, qty);
}

// ─────────────────────────────────────────────────────────────────────────────
//  SIGNAL PATH
// ─────────────────────────────────────────────────────────────────────────────

inline void ExecutionEngine::on_signal(const AlphaSignal &signal) {
  if (std::abs(signal.signal) <= 0.1)
    return;
  if (signal.confidence < 0.5)
    return;

  Order order;
  order.symbol = signal.symbol;
  order.side = (signal.signal > 0) ? Side::BUY : Side::SELL;
  order.quantity = static_cast<uint32_t>(signal.target_qty * signal.confidence);

  Side taker_side = order.side; // Capture before move

  if (signal.limit_price_bps > 0) {
    order.type = OrderType::LIMIT;
    order.price_bps = signal.limit_price_bps;
  } else {
    order.type = OrderType::MARKET;
    order.price_bps = 0;
  }

  try {
    submit_internal(std::move(order), taker_side);
  } catch (const std::exception &e) {
    LOG_ERR(std::string("[SIGNAL_REJECT] ") + e.what());
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  PUSH/PULL PATH
// ─────────────────────────────────────────────────────────────────────────────

inline void ExecutionEngine::on_push_message(IncomingMessage msg) {
  try {
    switch (msg.type) {

    case IncomingMessageType::ORDER: {
      Side taker_side = msg.order->order.side;
      submit_internal(std::move(msg.order->order), taker_side);
      break;
    }
    case IncomingMessageType::CANCEL:
      cancel_order(msg.cancel->symbol, msg.cancel->order_id);
      break;

    case IncomingMessageType::MARKET_DEPTH:
      on_market_depth(msg.depth->symbol, msg.depth->side, msg.depth->price_bps,
                      msg.depth->qty);
      break;

    case IncomingMessageType::UNKNOWN:
      break; // Already filtered — silently drop
    }
  } catch (const std::exception &e) {
    LOG_ERR(std::string("[PUSH_MSG_ERROR] ") + e.what());
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  ON_TRADE
// ─────────────────────────────────────────────────────────────────────────────

inline void ExecutionEngine::on_trade(const Trade &trade,
                                      const std::string &symbol) {
  // taker_side now read directly from trade.taker_side — set in execute_fill()
  // which has the taker Order in hand. No more pending_taker_sides_ lookup.
  risk_.on_trade(trade, trade.our_side);   // Post-trade: OUR position + PnL update
  publish_fill(trade, symbol);             // ZMQ PUB → Python dashboard
  persist_trade(trade, symbol);            // TimescaleDB via PgPool
}

// ─────────────────────────────────────────────────────────────────────────────
//  PUBLISH_FILL
// ─────────────────────────────────────────────────────────────────────────────

inline void ExecutionEngine::publish_fill(const Trade &trade,
                                          const std::string &symbol) {
  std::string payload = "fills " + trade_to_json(trade, symbol);
  std::lock_guard<std::mutex> lock(pub_mutex_);
  auto result =
      pub_socket_.send(zmq::buffer(payload), zmq::send_flags::dontwait);
  (void)result; // Fire and forget — dashboard may not be connected yet
}

// ─────────────────────────────────────────────────────────────────────────────
//  PERSIST_TRADE  (libpqxx — uncomment when dependency is available)
// ─────────────────────────────────────────────────────────────────────────────

inline void ExecutionEngine::persist_trade(const Trade &trade,
                                           const std::string &symbol) {
  // #include <pqxx/pqxx>   ← add to top of file
  // Install: apt install libpqxx-dev  OR  vcpkg install libpqxx
  //
  // IMPORTANT: In production use a connection POOL, not per-trade connections.
  // Opening a pqxx::connection is ~5ms — completely unacceptable on the trade
  // path. Pattern: create a pool of N connections at startup, acquire one here,
  // release after commit.
  //
  // try {
  //     pqxx::connection conn(config_.pg_connection_string);
  //     pqxx::work txn(conn);
  //     txn.exec_params(
  //         R"(INSERT INTO trades
  //            (trade_id, symbol, price_bps, quantity, maker_order_id,
  //            taker_order_id, executed_at) VALUES ($1, $2, $3, $4, $5, $6,
  //            NOW()) ON CONFLICT (trade_id) DO NOTHING)",
  //         static_cast<int64_t>(trade.trade_id),
  //         symbol,
  //         trade.price_bps,
  //         static_cast<int32_t>(trade.quantity),
  //         static_cast<int64_t>(trade.maker_order_id),
  //         static_cast<int64_t>(trade.taker_order_id)
  //     );
  //     txn.commit();   // RAII: auto-rollback if commit() never called
  // } catch (const pqxx::sql_error& e) {
  //     std::cerr << "[DB ERROR] " << e.what() << " query=" << e.query() <<
  //     "\n";
  // }

  (void)trade;
  (void)symbol;
}

// ─────────────────────────────────────────────────────────────────────────────
//  TRADE → JSON
// ─────────────────────────────────────────────────────────────────────────────

inline std::string
ExecutionEngine::trade_to_json(const Trade &trade,
                               const std::string &symbol) const {
  // system_clock::time_since_epoch() = milliseconds since Unix epoch
  // (1970-01-01). Previously used steady_clock which starts from boot —
  // produced garbage timestamps.
  auto epoch_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                      trade.timestamp.time_since_epoch())
                      .count();

  const char *side_str = (trade.taker_side == Side::BUY) ? "BUY" : "SELL";
  const char *our_side_str = (trade.our_side == Side::BUY) ? "BUY" : "SELL";

  std::ostringstream ss;
  ss << "{"
     << "\"trade_id\":" << trade.trade_id << ","
     << "\"symbol\":\"" << symbol << "\","
     << "\"price_bps\":" << trade.price_bps << ","
     << "\"price_dollars\":" << (trade.price_bps / 10000.0) << ","
     << "\"quantity\":" << trade.quantity << ","
     << "\"taker_side\":\"" << side_str << "\","
     << "\"our_side\":\"" << our_side_str << "\","
     << "\"maker_order_id\":" << trade.maker_order_id << ","
     << "\"taker_order_id\":" << trade.taker_order_id << ","
     << "\"timestamp_ms\":" << epoch_ms << "}";
  return ss.str();
}

inline const BookRouter *
ExecutionEngine::get_router(const std::string &symbol) const {
  std::shared_lock lock(routers_mutex_);
  auto it = routers_.find(symbol);
  return (it != routers_.end()) ? it->second.get() : nullptr;
}