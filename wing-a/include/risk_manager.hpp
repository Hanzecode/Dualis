#pragma once

#include "order_book.hpp" // For Order, Side, Trade definitions

#include <atomic> // std::atomic — thread-safe counters without a mutex
#include <cmath>  // std::abs — for position size checks
#include <stdexcept>
#include <string>
#include <unordered_map> // Position and PnL tracking by symbol

// ─────────────────────────────────────────────────────────────────────────────
//  RISK LIMITS STRUCT
//  All the configurable limits for one strategy/account.
//  Separating limits from the engine lets you reload them from config
//  without recompiling. In production: load from YAML/TOML at startup.
// ─────────────────────────────────────────────────────────────────────────────

struct RiskLimits {
  int32_t max_position_per_symbol; // e.g. 10,000 shares long or short
  int32_t max_total_notional_bps;  // Max total market value (in bps) across all
                                   // positions
  uint32_t max_order_size;         // Single order can't exceed this
  uint32_t max_orders_per_second;  // Rate limit — prevents runaway algos
  int64_t max_loss_bps;     // Daily loss limit — engine halts if breached
  double max_concentration; // Max % of portfolio in a single symbol (0.0–1.0)
};

// ─────────────────────────────────────────────────────────────────────────────
//  POSITION STRUCT
//  Tracks the current state of a position in one symbol.
//  All prices stored in basis points.
//
//  net_position > 0 = long (we own shares)
//  net_position < 0 = short (we owe shares)
// ─────────────────────────────────────────────────────────────────────────────

struct Position {
  std::string symbol;
  int32_t net_quantity;       // Positive = long, negative = short
  int64_t avg_cost_bps;       // Volume-weighted average cost of the position
  int64_t realised_pnl_bps;   // PnL from closed positions (locked in)
  int64_t unrealised_pnl_bps; // PnL from open position at current market price

  // Mark-to-market: recalculate unrealised PnL given a new market price
  void mark_to_market(int64_t market_price_bps) {
    // Unrealised PnL = (current price − average cost) × quantity
    unrealised_pnl_bps =
        static_cast<int64_t>(net_quantity) * (market_price_bps - avg_cost_bps);
    // static_cast<int64_t>: net_quantity is int32_t; multiplication could
    // overflow int32 × int64 → upcast first to avoid silent truncation
  }

  // Total PnL = realised + unrealised
  [[nodiscard]] int64_t total_pnl_bps() const {
    return realised_pnl_bps + unrealised_pnl_bps;
  }
};

// ─────────────────────────────────────────────────────────────────────────────
//  RISK MANAGER CLASS
//
//  Responsibilities:
//  1. Pre-trade checks: validate every order before it reaches the book
//  2. Post-trade updates: maintain positions and PnL after each fill
//  3. Breach detection: halt trading if limits are exceeded
//
//  Design: RiskManager is stateful — it accumulates positions over time.
//  It's injected into the execution engine, not a global singleton,
//  so tests can create isolated instances.
// ─────────────────────────────────────────────────────────────────────────────

class RiskManager {
public:
  // Reason an order was rejected — richer than a boolean return
  // Enum class: caller must write RiskRejectReason::POSITION_LIMIT, not just
  // POSITION_LIMIT
  enum class RejectionReason {
    APPROVED,       // Not a rejection — order is cleared
    ORDER_SIZE,     // Single order too large
    POSITION_LIMIT, // Would push net position beyond max
    NOTIONAL_LIMIT, // Portfolio notional too large
    LOSS_LIMIT,     // Daily loss limit breached — all orders blocked
    RATE_LIMIT,     // Too many orders per second
    CONCENTRATION,  // Would create too large a concentration in one symbol
    INVALID_PRICE,  // Limit price too far from mid (potential fat finger)
  };

  // Result of a pre-trade check
  struct CheckResult {
    RejectionReason reason;
    bool approved() const { return reason == RejectionReason::APPROVED; }
    // Converting constructor not needed — we'll use aggregate init or the
    // factory below
  };

  // Constructor — inject limits at construction time
  explicit RiskManager(RiskLimits limits);

  // ── Pre-trade check ──────────────────────────────────────────────────────
  // Call this BEFORE submitting to the order book.
  // Takes a const reference — we never modify the order during a check.
  [[nodiscard]] CheckResult check(const Order &order) const;

  // ── Post-trade update ────────────────────────────────────────────────────
  // Call this from the trade callback after each fill executes.
  void on_trade(const Trade &trade, Side taker_side);

  // ── Position queries ─────────────────────────────────────────────────────
  [[nodiscard]] const Position *get_position(const std::string &symbol) const;
  [[nodiscard]] int64_t total_pnl_bps() const;
  [[nodiscard]] bool is_halted() const { return halted_.load(); }
  // .load() reads the atomic safely

  // ── Admin ────────────────────────────────────────────────────────────────
  void reset_daily(); // Reset loss counters at start of day
  void update_market_price(const std::string &symbol, int64_t price_bps);

private:
  RiskLimits limits_;

  // Position map: symbol → position
  // mutable allows modification in const member functions
  // (needed if we implement lazy initialisation inside const getters)
  mutable std::unordered_map<std::string, Position> positions_;

  // std::atomic<bool>: the halted flag may be read/written from multiple
  // threads (the trade callback fires from the matching thread; the check runs
  // on the order thread) atomic guarantees the read/write is indivisible — no
  // partial reads of a bool.
  std::atomic<bool> halted_;
  std::atomic<int64_t>
      daily_loss_bps_; // Accumulated loss today (negative when losing)

  // Order rate limiting
  mutable std::atomic<uint32_t> orders_this_second_;
  mutable std::chrono::steady_clock::time_point rate_window_start_;

  // Helpers
  [[nodiscard]] int64_t
  total_notional_bps() const; // Sum of |position| × price across symbols
  void update_position_on_fill(
      const std::string &symbol,
      int32_t qty_delta, // Positive for buys, negative for sells
      int64_t fill_price_bps);
};

// ─────────────────────────────────────────────────────────────────────────────
//  IMPLEMENTATION (inline in header for brevity — in production, split to .cpp)
// ─────────────────────────────────────────────────────────────────────────────

inline RiskManager::RiskManager(RiskLimits limits)
    : limits_(limits), halted_(false), daily_loss_bps_(0),
      orders_this_second_(0),
      rate_window_start_(std::chrono::steady_clock::now()) {}

inline RiskManager::CheckResult RiskManager::check(const Order &order) const {
  // ── 1. Hard stop — if halted, reject everything ──────────────────────────
  if (halted_.load()) {
    // .load() with default memory_order_seq_cst: strongest guarantee,
    // ensures we see any prior writes to halted_ from other threads.
    return {RejectionReason::LOSS_LIMIT};
  }

  // ── 2. Order size check ─────────────────────────────────────────────────
  if (order.quantity > limits_.max_order_size) {
    return {RejectionReason::ORDER_SIZE};
  }

  // ── 3. Rate limit ───────────────────────────────────────────────────────
  auto now = std::chrono::steady_clock::now();
  auto elapsed =
      std::chrono::duration_cast<std::chrono::seconds>(now - rate_window_start_)
          .count();

  if (elapsed >= 1) {
    // New second — reset counter AND advance the window start time.
    // BUG FIX: previously rate_window_start_ was never updated here, so elapsed
    // was always >= 1 after the first second, causing orders_this_second_ to
    // reset on every single call — making the rate limit permanently
    // ineffective.
    orders_this_second_.store(0);
    rate_window_start_ = now; // FIX: advance window to current time
    // Note: non-atomic write to rate_window_start_ — TOCTOU race remains in
    // multi-threaded use. For true thread safety, use a mutex around the
    // window start + counter pair, or replace with a token-bucket algorithm.
  }

  if (orders_this_second_.load() >= limits_.max_orders_per_second) {
    return {RejectionReason::RATE_LIMIT};
  }
  orders_this_second_.fetch_add(
      1); // fetch_add is an atomic increment — no data race

  // ── 4. Position limit check ─────────────────────────────────────────────
  auto pos_it = positions_.find(order.symbol);
  int32_t current_net =
      (pos_it != positions_.end()) ? pos_it->second.net_quantity : 0;

  // Compute what the new net position would be
  int32_t delta = (order.side == Side::BUY)
                      ? static_cast<int32_t>(order.quantity)
                      : -static_cast<int32_t>(order.quantity);
  int32_t projected = current_net + delta;

  if (std::abs(projected) > limits_.max_position_per_symbol) {
    // std::abs from <cmath>: works on int32_t; returns non-negative value
    return {RejectionReason::POSITION_LIMIT};
  }

  return {RejectionReason::APPROVED}; // All checks passed
}

inline void RiskManager::on_trade(const Trade &trade, Side taker_side) {
  // Determine the quantity delta from the TAKER's perspective
  int32_t taker_delta = (taker_side == Side::BUY)
                            ? static_cast<int32_t>(trade.quantity)
                            : -static_cast<int32_t>(trade.quantity);

  // Update position for the taker
  update_position_on_fill(trade.symbol, taker_delta, trade.price_bps);

  // Check daily loss limit — halt if breached
  int64_t total_loss = daily_loss_bps_.load();
  if (total_loss < -limits_.max_loss_bps) {
    // Store true atomically — subsequent check() calls will see this
    // immediately
    halted_.store(true);
  }
}

inline void RiskManager::update_position_on_fill(const std::string &symbol,
                                                 int32_t qty_delta,
                                                 int64_t fill_price_bps) {
  Position &pos =
      positions_[symbol]; // Creates default-initialised Position if absent
  pos.symbol = symbol;

  int32_t old_qty = pos.net_quantity;
  int32_t new_qty = old_qty + qty_delta;

  if (old_qty == 0) {
    // Opening a new position — cost basis is just the fill price
    pos.avg_cost_bps = fill_price_bps;
  } else if ((old_qty > 0 && qty_delta > 0) || (old_qty < 0 && qty_delta < 0)) {
    // Adding to an existing position in the same direction
    // New average cost = VWAP of old position + new fill
    // avg = (old_qty × old_avg + qty_delta × fill_price) / new_qty
    // Cast to int64_t first to avoid overflow: 10000 × 1000000 = 10^9, fits
    // int64 fine
    pos.avg_cost_bps = (static_cast<int64_t>(old_qty) * pos.avg_cost_bps +
                        static_cast<int64_t>(qty_delta) * fill_price_bps) /
                       static_cast<int64_t>(new_qty);
  } else {
    // Reducing or flipping a position — realise PnL on the closed portion
    int32_t closed_qty = std::min(std::abs(old_qty), std::abs(qty_delta));
    // PnL per share = fill_price − avg_cost (positive if sell > cost, negative
    // if not)
    int64_t pnl_per_share = fill_price_bps - pos.avg_cost_bps;
    // Sign: if we were long (old_qty > 0) and sold, pnl = (sell - cost) × qty
    int64_t realised = static_cast<int64_t>(closed_qty) * pnl_per_share *
                       (old_qty > 0 ? 1 : -1);

    pos.realised_pnl_bps += realised;
    daily_loss_bps_.fetch_add(
        realised); // Atomic add — safe from trade callback thread
  }

  pos.net_quantity = new_qty;
}

inline const Position *
RiskManager::get_position(const std::string &symbol) const {
  auto it = positions_.find(symbol);
  if (it == positions_.end())
    return nullptr;   // Return nullptr rather than throwing
  return &it->second; // Return raw pointer — lifetime is the manager's
}

inline int64_t RiskManager::total_pnl_bps() const {
  int64_t total = 0;
  for (const auto &[symbol, pos] :
       positions_) { // Range-based for with structured binding
    total += pos.total_pnl_bps();
  }
  return total;
}

inline void RiskManager::update_market_price(const std::string &symbol,
                                             int64_t price_bps) {
  auto it = positions_.find(symbol);
  if (it != positions_.end()) {
    it->second.mark_to_market(
        price_bps); // Update unrealised PnL for this symbol
  }
}

inline void RiskManager::reset_daily() {
  daily_loss_bps_.store(0);
  halted_.store(false); // Re-enable trading at start of new day
}