#pragma once

// ─────────────────────────────────────────────────────────────────────────────
//  FLAT ORDER BOOK — array[20000], L3 cache optimised
//
//  PURPOSE vs OrderBook (std::map):
//  ┌─────────────────────┬──────────────────────┬────────────────────────┐
//  │                     │  FlatOrderBook       │  OrderBook (std::map)  │
//  ├─────────────────────┼──────────────────────┼────────────────────────┤
//  │  Price lookup       │  O(1) array index    │  O(log n) tree walk    │
//  │  Latency            │  ~4 ns               │  ~200–400 ns           │
//  │  Use case           │  Price discovery     │  Order lifecycle       │
//  │  Cancels            │  ❌ not supported    │  ✅ full support        │
//  │  Partial fills      │  ❌ qty only         │  ✅ per-order tracking  │
//  │  Memory layout      │  contiguous array    │  heap-scattered nodes  │
//  └─────────────────────┴──────────────────────┴────────────────────────┘
//
//  PRICE → INDEX MAPPING:
//  We define a "base price" at construction (e.g. $100.00 = 1000000 bps).
//  Array index = (price_bps - base_price_bps) + HALF_RANGE
//  This maps prices in [base - HALF_RANGE, base + HALF_RANGE] to [0, 20000).
//  HALF_RANGE = 10000 bps = $1.00 — covers ±$1 from the base price.
//  If the market moves outside this window, call recenter().
//
//  WHAT IT STORES:
//  Just total quantity at each price level — NOT individual order IDs.
//  This is intentional: for price discovery (mid price, spread, top-of-book
//  depth), quantity is all you need. For lifecycle (cancel, partial fill),
//  use OrderBook (std::map).
// ─────────────────────────────────────────────────────────────────────────────

#include "order_book.hpp" // Reuses: Side, Trade, Order, OrderStatus enum definitions

#include <algorithm> // std::max, std::min
#include <array> // std::array — fixed-size stack-allocated array, bounds-checkable
#include <cstdint>    // uint32_t, int64_t — fixed-width types
#include <cstring>    // std::memset — zero-initialise the quantity arrays fast
#include <functional> // std::function — trade callback type
#include <memory>     // std::unique_ptr / std::make_unique — heap scratch buffer in recenter()
#include <optional>   // std::optional — nullable return for best bid/ask
#include <stdexcept>  // std::out_of_range — thrown when price outside window

// ─────────────────────────────────────────────────────────────────────────────
//  COMPILE-TIME CONSTANTS
//  constexpr = evaluated at compile time — no runtime cost, no magic numbers
// ─────────────────────────────────────────────────────────────────────────────

// Total number of price slots in the array.
// FIX: was 20,000 slots (±$1 window) — far too narrow for real stocks.
// AAPL at $189 moves ±$3–5 per day, causing recenter() to fire every few
// seconds, each time triggering an O(n) 160 KB array copy on the hot path.
//
// New size: 400,000 slots × 4 bytes (uint32_t) = 1.56 MB per side = 3.1 MB
// total. This fits inside L3 cache (typically 8–32 MB) but not L2 (256 KB – 1
// MB). The ~4ns L2 claim no longer holds, but recenter() firing every second
// was far worse. L3 lookup is ~10–40ns — still excellent for price discovery.
inline constexpr int32_t BOOK_SLOTS = 400'000;

// Half the range — centre slot maps to the base price.
// HALF_RANGE = 200,000 bps = ±$20.00 window.
// Covers a full day's range for most US equities without recentering.
// For NVDA at $875: window is $855–$895 — recenters only on large gap moves.
inline constexpr int32_t HALF_RANGE = BOOK_SLOTS / 2; // = 200,000 bps = ±$20.00

// Sentinel: slot index meaning "no price found on this side"
inline constexpr int32_t NO_PRICE = -1;

// ─────────────────────────────────────────────────────────────────────────────
//  LEVEL SUMMARY
//  Lightweight struct returned from depth queries — just price + quantity.
//  No order IDs (this book doesn't track them).
// ─────────────────────────────────────────────────────────────────────────────

struct LevelSummary {
  int64_t price_bps;       // Price in basis points
  uint32_t total_quantity; // Aggregate quantity resting at this level
};

// ─────────────────────────────────────────────────────────────────────────────
//  FLAT ORDER BOOK CLASS
// ─────────────────────────────────────────────────────────────────────────────

class FlatOrderBook {
public:
  // ── Constructor ──────────────────────────────────────────────────────────
  // base_price_bps: the price the centre slot maps to.
  // Set this to the current mid-price of the instrument.
  // e.g. AAPL at $150.00 → base_price_bps = 15000000 (bps = price × 10000 here,
  // but match whatever convention the rest of your system uses)
  explicit FlatOrderBook(std::string symbol, int64_t base_price_bps);

  // ── Core operations ───────────────────────────────────────────────────────

  // Add quantity to a price level (new resting order or partial top-up).
  // Throws std::out_of_range if price is outside [base ± HALF_RANGE].
  void add_quantity(Side side, int64_t price_bps, uint32_t qty);

  // Remove quantity from a price level (cancel or fill).
  // Clamps to zero if qty > current (defensive — avoids unsigned wraparound).
  void remove_quantity(Side side, int64_t price_bps, uint32_t qty);

  // Set quantity at a price level directly (snapshot/reset from market data
  // feed).
  void set_quantity(Side side, int64_t price_bps, uint32_t qty);

  // ── Price discovery (the whole point of this structure) ───────────────────

  // Best bid: highest price with qty > 0.
  // Returns nullopt if no bids exist.
  // PERFORMANCE: scans from centre downward — typically 1–5 iterations
  // if the book is tight (spread < 5 ticks).
  [[nodiscard]] std::optional<int64_t> best_bid() const noexcept;

  // Best ask: lowest price with qty > 0.
  [[nodiscard]] std::optional<int64_t> best_ask() const noexcept;

  // Spread in bps. nullopt if either side is empty.
  [[nodiscard]] std::optional<int64_t> spread_bps() const noexcept;

  // Mid price (integer division). nullopt if either side empty.
  [[nodiscard]] std::optional<int64_t> mid_price_bps() const noexcept;

  // Quantity available at exactly this price on this side.
  // Returns 0 if price is outside range (safe — no throw).
  [[nodiscard]] uint32_t qty_at(Side side, int64_t price_bps) const noexcept;

  // Top N levels of depth on one side.
  // BUY  side: returns N highest prices (best bid first).
  // SELL side: returns N lowest prices (best ask first).
  [[nodiscard]] std::vector<LevelSummary> depth(Side side,
                                                int32_t levels = 5) const;

  // Total quantity on a side (entire book depth summed).
  [[nodiscard]] uint64_t total_qty(Side side) const noexcept;

  // ── Window management ─────────────────────────────────────────────────────

  // Shift the price window so new_base is at the centre.
  // Call this when best_bid or best_ask approaches the edge of the current
  // window. All existing quantity is preserved — slots are shifted relative to
  // new base.
  void recenter(int64_t new_base_price_bps);

  // Is this price within the current window?
  [[nodiscard]] bool in_range(int64_t price_bps) const noexcept;

  // ── Accessors ─────────────────────────────────────────────────────────────

  [[nodiscard]] const std::string &symbol() const { return symbol_; }
  [[nodiscard]] int64_t base_price_bps() const { return base_price_bps_; }

  // Reset: zero all quantities, keep base price. Used at start of session.
  void clear() noexcept;

private:
  std::string symbol_;
  int64_t base_price_bps_; // Price that maps to index HALF_RANGE

  // The two quantity arrays — one per side.
  // std::array<T, N>: stack-allocated, fixed size, no heap allocation.
  // Stored as plain uint32_t — 4 bytes per slot × 20000 = 80 KB each.
  // 'alignas(64)': aligns the array to a 64-byte cache line boundary.
  // Why? If the array starts mid-cache-line, the first access pulls two lines.
  // Alignment guarantees the hot region (slots near centre) is
  // cache-line-aligned.
  alignas(64)
      std::array<uint32_t, BOOK_SLOTS> bid_qty_; // qty at each bid price slot
  alignas(64)
      std::array<uint32_t, BOOK_SLOTS> ask_qty_; // qty at each ask price slot

  // Cached best-bid/ask slot indices — maintained incrementally.
  // Avoids a full scan of 20000 slots on every best_bid()/best_ask() call.
  // Invalidated to NO_PRICE when their slot empties; updated by add/remove.
  mutable int32_t best_bid_slot_; // Index of current best bid, or NO_PRICE
  mutable int32_t best_ask_slot_; // Index of current best ask, or NO_PRICE

  // ── Private helpers ───────────────────────────────────────────────────────

  // Convert a price in bps to an array slot index.
  // Returns NO_PRICE if outside the window.
  // 'noexcept': this is called in the hot path — must not throw.
  [[nodiscard]] int32_t price_to_slot(int64_t price_bps) const noexcept;

  // Convert a slot index back to its price.
  [[nodiscard]] int64_t slot_to_price(int32_t slot) const noexcept;

  // Scan for the best bid slot starting from a given index, scanning downward.
  // Called lazily when best_bid_slot_ is stale (NO_PRICE).
  [[nodiscard]] int32_t scan_best_bid() const noexcept;

  // Scan for the best ask slot starting from a given index, scanning upward.
  [[nodiscard]] int32_t scan_best_ask() const noexcept;

  // Select the correct array by side — avoids duplicating add/remove logic.
  // Returns a non-const reference for mutation.
  std::array<uint32_t, BOOK_SLOTS> &side_array(Side side) noexcept;

  // Returns a const reference (used in const member functions).
  const std::array<uint32_t, BOOK_SLOTS> &side_array(Side side) const noexcept;
};

// ─────────────────────────────────────────────────────────────────────────────
//  IMPLEMENTATION
//  Kept in the header (inline) so the compiler can inline hot-path calls
//  like price_to_slot() and qty_at() without a separate compilation unit.
//  In large projects, move to flat_order_book.cpp to reduce compile times.
// ─────────────────────────────────────────────────────────────────────────────

inline FlatOrderBook::FlatOrderBook(std::string symbol, int64_t base_price_bps)
    : symbol_(std::move(symbol)), base_price_bps_(base_price_bps),
      best_bid_slot_(NO_PRICE), best_ask_slot_(NO_PRICE) {
  // Zero-initialise both arrays in one shot.
  // std::memset is faster than a loop for large zero-fills —
  // the compiler/libc will use SIMD (AVX2/SSE) instructions automatically.
  bid_qty_.fill(0); // std::array::fill: sets every element to 0
  ask_qty_.fill(0);
}

// ─────────────────────────────────────────────────────────────────────────────
//  PRICE ↔ SLOT CONVERSION
//  This is the core of the O(1) lookup.
//
//  Example (base = 15000, HALF_RANGE = 10000):
//  price 15000 → slot 10000  (centre)
//  price 15001 → slot 10001
//  price 14999 → slot  9999
//  price  5000 → slot     0  (minimum)
//  price 25000 → slot 19999  (maximum)
// ─────────────────────────────────────────────────────────────────────────────

inline int32_t FlatOrderBook::price_to_slot(int64_t price_bps) const noexcept {
  // Cast to int64_t before arithmetic to avoid overflow on large prices.
  int64_t slot = (price_bps - base_price_bps_) + HALF_RANGE;

  // Bounds check: if outside [0, BOOK_SLOTS), return sentinel.
  if (slot < 0 || slot >= BOOK_SLOTS)
    return NO_PRICE;

  return static_cast<int32_t>(
      slot); // Safe: we just checked it fits in [0, 20000)
}

inline int64_t FlatOrderBook::slot_to_price(int32_t slot) const noexcept {
  // Inverse of price_to_slot: slot → price
  return base_price_bps_ + (static_cast<int64_t>(slot) - HALF_RANGE);
}

// ─────────────────────────────────────────────────────────────────────────────
//  SIDE ARRAY SELECTOR
//  Returns reference to bid_qty_ or ask_qty_ based on Side enum.
//  Allows add_quantity / remove_quantity to be side-agnostic one-liners.
// ─────────────────────────────────────────────────────────────────────────────

inline std::array<uint32_t, BOOK_SLOTS> &
FlatOrderBook::side_array(Side side) noexcept {
  return (side == Side::BUY) ? bid_qty_ : ask_qty_;
}

inline const std::array<uint32_t, BOOK_SLOTS> &
FlatOrderBook::side_array(Side side) const noexcept {
  return (side == Side::BUY) ? bid_qty_ : ask_qty_;
}

// ─────────────────────────────────────────────────────────────────────────────
//  ADD / REMOVE / SET QUANTITY
// ─────────────────────────────────────────────────────────────────────────────

inline void FlatOrderBook::add_quantity(Side side, int64_t price_bps,
                                        uint32_t qty) {
  int32_t slot = price_to_slot(price_bps);
  if (slot == NO_PRICE) {
    // Price is outside the current window — hard error, not silent drop.
    // In production: call recenter() before submitting, or log and skip.
    throw std::out_of_range(
        "FlatOrderBook::add_quantity: price " + std::to_string(price_bps) +
        " outside window [" + std::to_string(slot_to_price(0)) + ", " +
        std::to_string(slot_to_price(BOOK_SLOTS - 1)) + "]");
  }

  auto &arr = side_array(side);
  arr[slot] += qty; // Direct array write — one cache line touch if slot is hot

  // Update cached best index:
  // For BUY (bids): a higher slot = higher price = potentially new best bid
  // For SELL (asks): a lower slot = lower price = potentially new best ask
  if (side == Side::BUY) {
    if (best_bid_slot_ == NO_PRICE || slot > best_bid_slot_) {
      best_bid_slot_ = slot; // New or better best bid
    }
  } else {
    if (best_ask_slot_ == NO_PRICE || slot < best_ask_slot_) {
      best_ask_slot_ = slot; // New or better best ask
    }
  }
}

inline void FlatOrderBook::remove_quantity(Side side, int64_t price_bps,
                                           uint32_t qty) {
  int32_t slot = price_to_slot(price_bps);
  if (slot == NO_PRICE)
    return; // Outside range — silently ignore (defensive)

  auto &arr = side_array(side);

  // Clamp to zero — unsigned subtraction below zero wraps to a huge number
  // (UB-adjacent). This shouldn't happen if OrderBook feeds us correct data,
  // but be defensive.
  if (qty >= arr[slot]) {
    arr[slot] = 0; // Completely empty this slot
  } else {
    arr[slot] -= qty;
  }

  // If we just emptied the cached best slot, mark as stale.
  // We don't scan immediately — scan lazily only when best_bid()/best_ask() is
  // called. This is the key performance trick: don't pay for the scan unless
  // needed.
  if (side == Side::BUY && slot == best_bid_slot_ && arr[slot] == 0) {
    best_bid_slot_ = NO_PRICE; // Stale — will scan on next best_bid() call
  }
  if (side == Side::SELL && slot == best_ask_slot_ && arr[slot] == 0) {
    best_ask_slot_ = NO_PRICE;
  }
}

inline void FlatOrderBook::set_quantity(Side side, int64_t price_bps,
                                        uint32_t qty) {
  int32_t slot = price_to_slot(price_bps);
  if (slot == NO_PRICE)
    return;

  auto &arr = side_array(side);
  arr[slot] =
      qty; // Direct overwrite — used for snapshot updates from market data

  // Update best cache same as add_quantity
  if (qty > 0) {
    if (side == Side::BUY) {
      if (best_bid_slot_ == NO_PRICE || slot > best_bid_slot_)
        best_bid_slot_ = slot;
    } else {
      if (best_ask_slot_ == NO_PRICE || slot < best_ask_slot_)
        best_ask_slot_ = slot;
    }
  } else {
    // qty = 0: same logic as remove
    if (side == Side::BUY && slot == best_bid_slot_)
      best_bid_slot_ = NO_PRICE;
    if (side == Side::SELL && slot == best_ask_slot_)
      best_ask_slot_ = NO_PRICE;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  SCANNING HELPERS
//  Called lazily when the cached best slot is stale.
//  These are the only O(n) operations in this class — but n is typically
//  very small (< 10 ticks from centre in a liquid market).
// ─────────────────────────────────────────────────────────────────────────────

inline int32_t FlatOrderBook::scan_best_bid() const noexcept {
  // Scan from the highest possible slot downward until we find a non-zero qty.
  // Start from centre (HALF_RANGE) going down — bids are below mid.
  // In practice we rarely scan more than a few slots.
  for (int32_t i = BOOK_SLOTS - 1; i >= 0; --i) {
    if (bid_qty_[i] > 0)
      return i; // Found: this is the best bid slot
  }
  return NO_PRICE; // No bids at all
}

inline int32_t FlatOrderBook::scan_best_ask() const noexcept {
  for (int32_t i = 0; i < BOOK_SLOTS; ++i) {
    if (ask_qty_[i] > 0)
      return i; // Found: this is the best ask slot
  }
  return NO_PRICE;
}

// ─────────────────────────────────────────────────────────────────────────────
//  BEST BID / ASK  — the hot path
//  Typical execution: one branch check + one array read = ~4 ns
// ─────────────────────────────────────────────────────────────────────────────

inline std::optional<int64_t> FlatOrderBook::best_bid() const noexcept {
  // If cache is valid (NO_PRICE means stale), use it directly — single array
  // read.
  if (best_bid_slot_ == NO_PRICE) {
    // Cache is stale — scan to find the new best bid
    best_bid_slot_ = scan_best_bid(); // Updates mutable cache
  }
  if (best_bid_slot_ == NO_PRICE)
    return std::nullopt; // Book is empty
  return slot_to_price(best_bid_slot_);
}

inline std::optional<int64_t> FlatOrderBook::best_ask() const noexcept {
  if (best_ask_slot_ == NO_PRICE) {
    best_ask_slot_ = scan_best_ask();
  }
  if (best_ask_slot_ == NO_PRICE)
    return std::nullopt;
  return slot_to_price(best_ask_slot_);
}

inline std::optional<int64_t> FlatOrderBook::spread_bps() const noexcept {
  auto bid = best_bid();
  auto ask = best_ask();
  if (!bid || !ask)
    return std::nullopt;
  return *ask - *bid; // Dereference both optionals — both have values here
}

inline std::optional<int64_t> FlatOrderBook::mid_price_bps() const noexcept {
  auto bid = best_bid();
  auto ask = best_ask();
  if (!bid || !ask)
    return std::nullopt;
  return (*bid + *ask) / 2;
}

// ─────────────────────────────────────────────────────────────────────────────
//  DEPTH QUERY
//  Returns the top N levels as a vector of LevelSummary.
//  Used by the React dashboard and the Python analytics platform.
// ─────────────────────────────────────────────────────────────────────────────

inline std::vector<LevelSummary> FlatOrderBook::depth(Side side,
                                                      int32_t levels) const {
  std::vector<LevelSummary> result;
  result.reserve(levels); // Pre-allocate — avoids reallocation inside the loop

  const auto &arr = side_array(side);

  if (side == Side::BUY) {
    // Bids: scan from high → low (best bid first)
    for (int32_t i = BOOK_SLOTS - 1;
         i >= 0 && static_cast<int32_t>(result.size()) < levels; --i) {
      if (arr[i] > 0) {
        result.push_back({slot_to_price(i), arr[i]});
      }
    }
  } else {
    // Asks: scan from low → high (best ask first)
    for (int32_t i = 0;
         i < BOOK_SLOTS && static_cast<int32_t>(result.size()) < levels; ++i) {
      if (arr[i] > 0) {
        result.push_back({slot_to_price(i), arr[i]});
      }
    }
  }

  return result; // NRVO (Named Return Value Optimisation): compiler elides the
                 // copy
}

inline uint32_t FlatOrderBook::qty_at(Side side, int64_t price_bps) const noexcept {
  int32_t slot = price_to_slot(price_bps);
  if (slot == NO_PRICE)
    return 0;                    // Outside window — treat as no liquidity
  return side_array(side)[slot]; // Single array read — ~1 ns if cache hot
}

inline uint64_t FlatOrderBook::total_qty(Side side) const noexcept {
  uint64_t total = 0;
  for (const auto &q : side_array(side)) {
    total +=
        q; // Sum all slots — O(n) but only called for diagnostics, not hot path
  }
  return total;
}

// ─────────────────────────────────────────────────────────────────────────────
//  RECENTER
//  Shifts the window so new_base is at slot HALF_RANGE.
//  Preserves all quantities that still fit in the new window.
//  Called when the market moves more than ~HALF_RANGE bps from the current
//  base.
// ─────────────────────────────────────────────────────────────────────────────

inline void FlatOrderBook::recenter(int64_t new_base_price_bps) {
  // How many slots to shift?
  // positive shift_slots = market moved up (new base is higher)
  // negative shift_slots = market moved down
  int64_t shift_slots = (new_base_price_bps - base_price_bps_);
  // No shift needed if delta is zero
  if (shift_slots == 0)
    return;

  // Rebuild both arrays shifted by shift_slots.
  // Slots that fall outside [0, BOOK_SLOTS) after shifting are discarded
  // (liquidity gone).
  // NOTE: BOOK_SLOTS == 400,000, so a std::array<uint32_t, BOOK_SLOTS> is
  // ~1.53 MB. That is far bigger than the default 512 KiB pthread stack
  // used by the ZMQ receiver threads on macOS — a stack-allocated scratch
  // buffer here reliably blows the stack (SIGBUS/EXC_BAD_ACCESS) the first
  // time a real market-depth message triggers a recenter(). Heap-allocate
  // the scratch buffer instead; recenter() is rare (not in the hot path),
  // so the allocation cost is a non-issue.
  auto shift_array = [&](std::array<uint32_t, BOOK_SLOTS> &arr) {
    auto shifted = std::make_unique<std::array<uint32_t, BOOK_SLOTS>>();
    shifted->fill(0);

    for (int32_t old_slot = 0; old_slot < BOOK_SLOTS; ++old_slot) {
      if (arr[old_slot] == 0)
        continue; // Skip empty slots — fast path

      // The old slot maps to a new slot after the shift
      int64_t new_slot = static_cast<int64_t>(old_slot) - shift_slots;

      // Only copy if the new slot is still inside the array
      if (new_slot >= 0 && new_slot < BOOK_SLOTS) {
        (*shifted)[static_cast<int32_t>(new_slot)] = arr[old_slot];
      }
      // Otherwise: this price level fell off the edge — quantity lost.
      // In practice, OrderBook (std::map) still tracks these orders correctly.
      // FlatOrderBook is only used for price discovery, not order lifecycle.
    }
    arr = std::move(*shifted); // Copy shifted contents back into the
                                // fixed-size member array.
  };

  shift_array(bid_qty_);
  shift_array(ask_qty_);

  // Update base price to the new centre
  base_price_bps_ = new_base_price_bps;

  // Invalidate cached best slots — they need to be recomputed after shift
  best_bid_slot_ = NO_PRICE;
  best_ask_slot_ = NO_PRICE;
}

inline bool FlatOrderBook::in_range(int64_t price_bps) const noexcept {
  return price_to_slot(price_bps) != NO_PRICE;
}

inline void FlatOrderBook::clear() noexcept {
  bid_qty_.fill(0);
  ask_qty_.fill(0);
  best_bid_slot_ = NO_PRICE;
  best_ask_slot_ = NO_PRICE;
}