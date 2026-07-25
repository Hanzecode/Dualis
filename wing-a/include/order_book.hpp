#pragma once // Include guard — prevents this header being processed twice in
             // one translation unit

#include <chrono> // std::chrono::system_clock — wall-clock time for timestamps (Unix epoch)
#include <cstdint> // uint64_t, int64_t — fixed-width integers; essential in finance to avoid platform surprises
#include <functional> // std::function — used for trade callback type (lets callers inject any callable)
#include <map> // std::map — used for price levels (sorted automatically, O(log n) insert/lookup)
#include <optional> // std::optional — return type when a match might not exist (cleaner than returning -1 or nullptr)
#include <queue> // std::queue — FIFO queue for orders at the same price level (price-time priority)
#include <stdexcept> // std::runtime_error, std::invalid_argument — for meaningful error messages
#include <unordered_map> // std::unordered_map — used for order lookup by ID (O(1) average)

// ─────────────────────────────────────────────────────────────────────────────
//  ENUMERATIONS
//  Using 'enum class' (C++11) instead of plain 'enum':
//  - Plain enum: Side::BUY and Side::SELL pollute the surrounding namespace
//  - Enum class: you must write Side::BUY, preventing accidental comparisons
//    like (BUY == OrderType::LIMIT) compiling silently
// ─────────────────────────────────────────────────────────────────────────────

enum class Side {
  BUY, // Bid side — willing to buy at price or lower
  SELL // Ask side — willing to sell at price or higher
};

enum class OrderType {
  LIMIT, // Rests in the book at a specific price until filled or cancelled
  MARKET // Fills immediately at best available price; never rests in book
};

enum class OrderStatus {
  NEW,              // Order received, not yet processed
  OPEN,             // Order is active and acknowledged by the engine
  PARTIALLY_FILLED, // Some quantity filled; remainder still in book
  FILLED,           // Fully executed — no remaining quantity
  CANCELLED         // Removed from book without full execution
};

// ─────────────────────────────────────────────────────────────────────────────
//  ORDER STRUCT
//  Plain data structure — no methods, just fields.
//  Using int64_t for price: we store price in basis points (e.g. 10050 =
//  $100.50) to avoid floating-point rounding errors. Never store prices as
//  double.
// ─────────────────────────────────────────────────────────────────────────────

struct Order {
  uint64_t order_id; // Unique identifier — assigned by the engine, never reused
  std::string
      symbol; // Ticker symbol e.g. "AAPL" — could be an enum in prod for speed
  Side side;  // BUY or SELL
  OrderType type;    // LIMIT or MARKET
  int64_t price_bps; // Price in basis points (100ths of a cent) — avoids float
                     // imprecision
  uint32_t quantity; // Number of shares/contracts requested
  uint32_t filled_qty; // How much has been matched so far (starts at 0)
  OrderStatus status;  // Current lifecycle state
  std::chrono::system_clock::time_point
      timestamp; // Wall-clock time order entered engine
                 // system_clock = Unix epoch — correct for audit logs and JSON
                 // timestamps (steady_clock starts from boot, not Unix epoch —
                 // wrong for absolute times)

  // Remaining quantity to fill — computed on the fly, not stored (derived data)
  // 'const' member function = does not modify *this; callable on const Order&
  [[nodiscard]] uint32_t remaining() const {
    return quantity - filled_qty; // Total requested minus already filled
  }

  // [[nodiscard]] (C++17 attribute): if caller ignores the return value,
  // the compiler emits a warning. Useful — accidentally ignoring remaining()
  // in matching logic would be a silent bug.
};

// ─────────────────────────────────────────────────────────────────────────────
//  TRADE STRUCT
//  Represents a completed match between two orders.
//  Emitted by the matching engine; consumed by risk, PnL, and reporting
//  systems.
// ─────────────────────────────────────────────────────────────────────────────

struct Trade {
  uint64_t trade_id;       // Unique trade identifier
  uint64_t maker_order_id; // The order that was resting in the book (passive)
  uint64_t taker_order_id; // The order that arrived and triggered the match
                           // (aggressive)
  Side taker_side;         // Side of the TAKER order (BUY or SELL)
                   // FIX: was previously tracked via pending_taker_sides_
                   // sentinel in ExecutionEngine — a data race under concurrent
                   // submissions. The matching engine knows taker_side at fill
                   // time (it has the taker Order in hand) — recording it here
                   // is the clean fix.
  std::string symbol;
  int64_t price_bps; // Price at which the trade executed
  uint32_t quantity; // Number of shares/contracts exchanged
  std::chrono::system_clock::time_point timestamp;
  // system_clock = wall-clock (Unix epoch) — correct for JSON timestamps sent
  // to dashboard. steady_clock starts from boot and produces meaningless
  // epoch_ms values.
};

// ─────────────────────────────────────────────────────────────────────────────
//  PRICE LEVEL
//  All orders at the same price on the same side form a "level".
//  Orders within a level are matched in FIFO (time) order.
//  Using std::queue: push_back to enqueue, front() + pop() to dequeue FIFO.
// ─────────────────────────────────────────────────────────────────────────────

struct PriceLevel {
  int64_t price_bps;              // The price this level represents
  std::queue<uint64_t> order_ids; // FIFO queue of order IDs at this level
  uint32_t
      total_quantity; // Sum of remaining quantity across all orders here
                      // Maintained incrementally — avoids O(n) recomputation
};

// ─────────────────────────────────────────────────────────────────────────────
//  ORDER BOOK CLASS
//
//  DATA STRUCTURES:
//  - Bids: std::map<int64_t, PriceLevel, std::greater<int64_t>>
//    Sorted descending — best bid is map.begin() (highest price)
//  - Asks: std::map<int64_t, PriceLevel, std::less<int64_t>>  (default)
//    Sorted ascending — best ask is map.begin() (lowest price)
//  - Orders: std::unordered_map<uint64_t, Order>
//    Flat lookup by ID — O(1) cancel/modify without scanning the sorted maps
//
//  WHY NOT A SORTED ARRAY?
//  Insertion into a sorted array is O(n) due to shifting. std::map gives O(log
//  n) insert/delete. For a book with ~1000 active levels, log2(1000) ≈ 10
//  comparisons.
// ─────────────────────────────────────────────────────────────────────────────

class OrderBook {
public:
  // ── Type aliases ────────────────────────────────────────────────────────
  // 'using' (C++11) is preferred over 'typedef' — more readable for templates
  // and supports partial template specialisation

  // Callback invoked every time a trade executes
  // std::function<void(const Trade&)> wraps any callable: lambda, functor, free
  // function
  using TradeCallback = std::function<void(const Trade &)>;

  // Descending comparator for bid side: best (highest) bid at front
  using BidMap = std::map<int64_t, PriceLevel, std::greater<int64_t>>;

  // Ascending comparator for ask side (std::map default): best (lowest) ask at
  // front
  using AskMap = std::map<int64_t, PriceLevel>;

  // ── Constructor ─────────────────────────────────────────────────────────
  // 'explicit' prevents implicit conversion: OrderBook("AAPL") from a string
  // context
  explicit OrderBook(std::string symbol, TradeCallback on_trade);

  // ── Public interface ────────────────────────────────────────────────────

  // Submit a new order — returns the assigned order ID
  // Takes Order by value then moves it in — caller's Order is consumed
  uint64_t submit(Order order);

  // Cancel an open order — returns false if order not found or already done
  bool cancel(uint64_t order_id);

  // Query the best bid price (highest). Returns empty optional if no bids.
  // std::optional avoids magic sentinel values like -1 or 0 for "no price"
  [[nodiscard]] std::optional<int64_t> best_bid() const;

  // Query the best ask price (lowest). Returns empty optional if no asks.
  [[nodiscard]] std::optional<int64_t> best_ask() const;

  // Spread in basis points — optional because either side could be empty
  [[nodiscard]] std::optional<int64_t> spread_bps() const;

  // Mid-price in basis points
  [[nodiscard]] std::optional<int64_t> mid_price_bps() const;

  // Total depth (number of price levels) on each side — useful for diagnostics
  [[nodiscard]] size_t bid_levels() const { return bids_.size(); }
  [[nodiscard]] size_t ask_levels() const { return asks_.size(); }

  // Look up any order by ID — returns const reference via optional
  [[nodiscard]] std::optional<std::reference_wrapper<const Order>>
  find_order(uint64_t order_id) const;

  // Symbol this book manages
  [[nodiscard]] const std::string &symbol() const { return symbol_; }

private:
  // ── State ────────────────────────────────────────────────────────────────
  std::string symbol_; // e.g. "AAPL"
  BidMap bids_;        // Bid side, best (highest) price first
  AskMap asks_;        // Ask side, best (lowest) price first
  std::unordered_map<uint64_t, Order> orders_; // All live orders by ID
  TradeCallback on_trade_; // Emitted for every completed match
  uint64_t next_order_id_; // Monotonically increasing order ID counter
  uint64_t next_trade_id_; // Monotonically increasing trade ID counter

  // ── Private helpers ──────────────────────────────────────────────────────

  // Core matching loop — called after a new order is placed
  // 'Order&' is a non-const reference: we modify filled_qty and status in place
  void match(Order &incoming);

  // Attempt to match a LIMIT order against the opposite side
  void match_limit(Order &incoming);

  // Attempt to match a MARKET order — sweeps through levels until filled
  void match_market(Order &incoming);

  // Add an unmatched (or partially matched) limit order to the book
  void add_to_book(const Order &order);

  // Remove a price level if it has become empty
  // Template lets one function handle both BidMap and AskMap -> easier to read
  // and maintain
  template <typename MapType>
  void remove_empty_level(MapType &side_map, int64_t price_bps);

  // Execute a fill between two orders — updates both, emits Trade callback
  void execute_fill(Order &maker, Order &taker, uint32_t fill_qty);

  // Generate next monotonic order/trade IDs (thread-unsafe by design —
  // if multi-threaded, replace with std::atomic<uint64_t>)
  uint64_t next_order_id() { return ++next_order_id_; }
  uint64_t next_trade_id() { return ++next_trade_id_; }
};