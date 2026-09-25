#pragma once

// ─────────────────────────────────────────────────────────────────────────────
//  BOOK ROUTER — dual order book orchestrator
//
//  Your architecture diagram shows:
//    ZeroMQ PUSH/PULL (Python → C++)
//           ↓
//    C++ matching engine — dual order book
//    ┌─────────────────────┬─────────────────────────┐
//    │  FlatOrderBook      │  OrderBook (std::map)   │
//    │  price discovery    │  order lifecycle        │
//    │  L2 cache, ~4ns     │  cancel, partial fills  │
//    └─────────────────────┴─────────────────────────┘
//
//  BookRouter owns one instance of each book per symbol and keeps them
//  synchronised:
//
//  ON ORDER SUBMIT:
//    1. Route the order to OrderBook (std::map) for lifecycle tracking
//    2. When OrderBook fires its trade callback, mirror the fill
//       into FlatOrderBook (remove filled quantity)
//
//  ON MARKET DATA UPDATE (from external feed):
//    1. Update FlatOrderBook directly (fast snapshot update)
//    2. Do NOT touch OrderBook — it manages only our own orders
//
//  PRICE DISCOVERY QUERIES:
//    → Always ask FlatOrderBook — it's the fast path
//
//  ORDER LIFECYCLE QUERIES:
//    → Always ask OrderBook — it has per-order detail
// ─────────────────────────────────────────────────────────────────────────────

#include "flat_order_book.hpp" // FlatOrderBook, LevelSummary
#include "order_book.hpp"      // OrderBook, Order, Trade, Side

#include <functional> // std::function — trade callback type
#include <memory>     // std::unique_ptr — owns both book instances
#include <stdexcept>  // std::invalid_argument
#include <string>

// ─────────────────────────────────────────────────────────────────────────────
//  BOOK ROUTER CLASS
// ─────────────────────────────────────────────────────────────────────────────

class BookRouter {
public:
  // Trade callback — same signature as OrderBook's callback.
  // Fired after both books have been updated for a fill.
  using TradeCallback = std::function<void(const Trade &)>;

  // ── Constructor ──────────────────────────────────────────────────────────
  // symbol:            e.g. "AAPL"
  // base_price_bps:    starting centre price for FlatOrderBook window
  //                    Set to the current market mid price at startup.
  // on_trade:          callback fired after every matched fill
  BookRouter(std::string symbol, int64_t base_price_bps,
             TradeCallback on_trade);

  // ── Order submission (goes to OrderBook, mirrors fills to FlatOrderBook) ─

  // Submit a new order — returns assigned order ID.
  // Pre-trade risk check should happen BEFORE calling this.
  uint64_t submit(Order order);

  // Cancel a resting order.
  // Also removes its quantity from FlatOrderBook.
  bool cancel(uint64_t order_id);

  // ── Market data feed (goes to FlatOrderBook only) ─────────────────────────

  // Apply a full depth snapshot from the exchange feed.
  // Replaces the FlatOrderBook's view of a price level.
  // Does NOT affect OrderBook — our own orders are tracked separately.
  void on_market_depth(Side side, int64_t price_bps, uint32_t qty);

  // Shift the FlatOrderBook's price window when market moves.
  // Call when: best_bid or best_ask approaches the edge of the current window.
  void recenter(int64_t new_base_price_bps);

  // ── Price discovery — delegates to FlatOrderBook (~4 ns) ─────────────────

  [[nodiscard]] std::optional<int64_t> best_bid() const noexcept;
  [[nodiscard]] std::optional<int64_t> best_ask() const noexcept;
  [[nodiscard]] std::optional<int64_t> spread_bps() const noexcept;
  [[nodiscard]] std::optional<int64_t> mid_price_bps() const noexcept;
  [[nodiscard]] uint32_t qty_at(Side side, int64_t price_bps) const noexcept;

  // Top N depth levels from FlatOrderBook
  [[nodiscard]] std::vector<LevelSummary> depth(Side side,
                                                int32_t levels = 5) const;

  // ── Order lifecycle — delegates to OrderBook ──────────────────────────────

  [[nodiscard]] std::optional<std::reference_wrapper<const Order>>
  find_order(uint64_t order_id) const;

  [[nodiscard]] size_t
  bid_levels() const; // Number of price levels in OrderBook
  [[nodiscard]] size_t ask_levels() const;

  // ── Direct book access (read-only) ────────────────────────────────────────

  [[nodiscard]] const FlatOrderBook &flat_book() const { return *flat_book_; }
  [[nodiscard]] const OrderBook &order_book() const { return *order_book_; }
  [[nodiscard]] const std::string &symbol() const { return symbol_; }

private:
  std::string symbol_;

  // Owned by BookRouter — unique_ptr because OrderBook is non-copyable
  // (holds a std::function callback that may capture non-copyable state)
  std::unique_ptr<FlatOrderBook> flat_book_;
  std::unique_ptr<OrderBook> order_book_;

  // The external callback (e.g. ExecutionEngine::on_trade)
  TradeCallback external_callback_;

  // Internal trade handler: fired by OrderBook, mirrors fill to FlatOrderBook,
  // then forwards to external_callback_.
  void on_trade_internal(const Trade &trade);
};

// ─────────────────────────────────────────────────────────────────────────────
//  IMPLEMENTATION
// ─────────────────────────────────────────────────────────────────────────────

inline BookRouter::BookRouter(std::string symbol, int64_t base_price_bps,
                              TradeCallback on_trade)
    : symbol_(std::move(symbol)), external_callback_(std::move(on_trade)) {
  // Construct FlatOrderBook — simple, just needs symbol and base price
  flat_book_ = std::make_unique<FlatOrderBook>(symbol_, base_price_bps);

  // Construct OrderBook — inject a lambda that calls our internal handler.
  // 'this' is captured by pointer — safe because flat_book_ and
  // external_callback_ are members of *this, which outlives the lambda.
  order_book_ = std::make_unique<OrderBook>(
      symbol_, [this](const Trade &t) { this->on_trade_internal(t); }
      // Lambda: captures 'this', calls on_trade_internal for every fill
  );
}

// ─────────────────────────────────────────────────────────────────────────────
//  SUBMIT
//  Routes order to OrderBook. FlatOrderBook is updated indirectly via
//  the trade callback when a fill occurs.
//
//  Note: we also add the order's quantity to FlatOrderBook when it RESTS
//  in the book (so the flat book reflects our own resting orders too).
//  This keeps the flat book's depth view accurate.
// ─────────────────────────────────────────────────────────────────────────────

inline uint64_t BookRouter::submit(Order order) {
  // Capture price and quantity before moving the order into the book.
  // After submit(), 'order' may have been moved-from — don't rely on its
  // fields.
  int64_t price = order.price_bps;
  Side side = order.side;
  bool is_limit = (order.type == OrderType::LIMIT);

  // Submit to OrderBook (std::map) — this runs the matching loop
  uint64_t id = order_book_->submit(std::move(order));
  // After submit, if the order was partially or fully matched, the trade
  // callback (on_trade_internal) has already been called and has already
  // updated flat_book_. We only add the REMAINING qty to flat_book_.

  // If it's a limit order that rested (at least partially), add its
  // remaining quantity to the flat book so depth() reflects our resting qty.
  if (is_limit) {
    auto opt = order_book_->find_order(id);
    uint32_t remaining = opt.has_value() ? opt->get().remaining() : 0;
    if (remaining > 0) {
      try {
        // Check window before adding — auto-recenter if needed
        if (!flat_book_->in_range(price)) {
          flat_book_->recenter(price); // Re-centre on the order price
        }
        flat_book_->add_quantity(side, price, remaining);
      } catch (const std::out_of_range &) {
        // Price outside window even after recenter attempt — skip.
        // FlatOrderBook depth will be slightly stale; acceptable.
      }
    }
  }

  return id;
}

// ─────────────────────────────────────────────────────────────────────────────
//  CANCEL
//  Cancels in OrderBook and removes the quantity from FlatOrderBook.
// ─────────────────────────────────────────────────────────────────────────────

inline bool BookRouter::cancel(uint64_t order_id) {
  // Look up the order BEFORE cancelling — we need its price/qty/side
  // to remove from FlatOrderBook. After cancel(), it's gone from OrderBook.
  auto opt = order_book_->find_order(order_id);
  if (!opt.has_value())
    return false;

  const Order &o = opt->get(); // Reference to the order (via reference_wrapper)
  int64_t price = o.price_bps;
  uint32_t remaining = o.remaining();
  Side side = o.side;

  // Cancel in OrderBook (std::map)
  bool cancelled = order_book_->cancel(order_id);
  if (!cancelled)
    return false;

  // Mirror: remove the cancelled order's resting quantity from FlatOrderBook
  if (flat_book_->in_range(price)) {
    flat_book_->remove_quantity(side, price, remaining);
  }

  return true;
}

// ─────────────────────────────────────────────────────────────────────────────
//  ON_TRADE_INTERNAL
//  Called by OrderBook every time two orders match.
//  We use the fill to:
//  1. Remove filled quantity from FlatOrderBook (keep depth view accurate)
//  2. Forward the trade to the external callback (risk manager, ZMQ publisher)
// ─────────────────────────────────────────────────────────────────────────────

inline void BookRouter::on_trade_internal(const Trade &trade) {
  // The trade executed at trade.price_bps — remove that qty from both sides.
  // Why both sides? Because both the maker and taker had resting or incoming
  // qty at this price. The flat book tracks aggregate depth.

  // Maker was resting — remove from the opposite side of the taker.
  // We don't know which side the maker was on from the Trade alone,
  // so remove from both (if qty > pool, remove_quantity clamps to 0).
  if (flat_book_->in_range(trade.price_bps)) {
    flat_book_->remove_quantity(Side::BUY, trade.price_bps, trade.quantity);
    flat_book_->remove_quantity(Side::SELL, trade.price_bps, trade.quantity);
    // Removing from both is safe: whichever side had nothing at this
    // price will just clamp to 0 (no-op). The side that had the maker
    // will have its qty decremented correctly.
  }

  // Forward to external callback (ExecutionEngine → risk → ZMQ PUB)
  if (external_callback_) {
    external_callback_(trade);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  MARKET DATA FEED — FlatOrderBook, PLUS a crossing check against our own
//  resting orders in OrderBook.
//
//  BUG FIX (was: "market data update never touches OrderBook"):
//  Previously a resting order of ours could become marketable purely because
//  the EXTERNAL market moved through it (e.g. our resting BUY at $223.77
//  while AlpacaFeed's live ask fell to $222.53) and it would NEVER fill —
//  match()/match_limit() only run inside OrderBook::submit(), which is only
//  called when a NEW order arrives. Market depth ticks landed in FlatOrderBook
//  only, so a crossed book (flat_book best_bid > best_ask) could sit
//  indefinitely with $0 PnL and zero fills, even though real fills should
//  occur whenever the market trades through our resting price.
//
//  FIX: after applying the snapshot to FlatOrderBook, check whether this
//  price tick crosses our own best resting order in OrderBook. If so,
//  synthesize a MARKET order representing the external market side that
//  crossed us, and submit() it — this reuses the existing, already-correct
//  match_market()/execute_fill() pipeline (fills at OUR resting price, fires
//  the same Trade callback that updates PnL/risk/ZMQ PUB) instead of adding
//  a second matching code path.
//
//  SIMPLIFICATION: match_market() sweeps price levels until the synthetic
//  order's quantity is exhausted, bounded only by the incoming depth qty —
//  it does not stop at price_bps if we have multiple resting levels deeper
//  than the level that actually crossed. For a single-venue paper/dev feed
//  this is an acceptable approximation; a venue with real level-by-level
//  depth reconciliation would want to bound the sweep by price_bps too.
// ─────────────────────────────────────────────────────────────────────────────

inline void BookRouter::on_market_depth(Side side, int64_t price_bps,
                                        uint32_t qty) {
  // Auto-recenter if the incoming price is outside our current window.
  // This handles the case where the market has moved since last recenter.
  if (!flat_book_->in_range(price_bps)) {
    // Use the incoming price as the new centre — it's where the market is now.
    flat_book_->recenter(price_bps);
  }
  // set_quantity: snapshot update — replaces whatever was there before.
  // Used for L2 feed updates (exchange sends full level qty, not delta).
  flat_book_->set_quantity(side, price_bps, qty);

  if (qty == 0) return; // Nothing to cross with.

  // side == SELL  → this tick is the market's ASK. If it fell to/through our
  //                 best resting BID, the external seller would sell to us.
  //                 The aggressor (taker) is that seller, so the synthetic
  //                 order is a SELL — match_market() will walk our bids_.
  // side == BUY   → this tick is the market's BID. If it rose to/through our
  //                 best resting ASK, the external buyer would buy from us.
  //                 The synthetic taker order is a BUY — walks our asks_.
  if (side == Side::SELL) {
    auto our_bid = order_book_->best_bid();
    if (our_bid.has_value() && price_bps <= *our_bid) {
      Order synthetic{};
      synthetic.symbol = symbol_;
      synthetic.side = Side::SELL;
      synthetic.type = OrderType::MARKET;
      synthetic.price_bps = 0; // Ignored by match_market — fills at maker price.
      synthetic.quantity = qty;
      synthetic.is_synthetic = true; // stands in for the external market
      submit(std::move(synthetic)); // → OrderBook::submit → match_market → execute_fill
    }
  } else {
    auto our_ask = order_book_->best_ask();
    if (our_ask.has_value() && price_bps >= *our_ask) {
      Order synthetic{};
      synthetic.symbol = symbol_;
      synthetic.side = Side::BUY;
      synthetic.type = OrderType::MARKET;
      synthetic.price_bps = 0;
      synthetic.quantity = qty;
      synthetic.is_synthetic = true; // stands in for the external market
      submit(std::move(synthetic));
    }
  }
}

inline void BookRouter::recenter(int64_t new_base_price_bps) {
  flat_book_->recenter(new_base_price_bps);
}

// ── Price discovery delegates ────────────────────────────────────────────────
// All delegate to FlatOrderBook — single indirection, still ~4–10 ns total

inline std::optional<int64_t> BookRouter::best_bid() const noexcept {
  return flat_book_->best_bid();
}
inline std::optional<int64_t> BookRouter::best_ask() const noexcept {
  return flat_book_->best_ask();
}
inline std::optional<int64_t> BookRouter::spread_bps() const noexcept {
  return flat_book_->spread_bps();
}
inline std::optional<int64_t> BookRouter::mid_price_bps() const noexcept {
  return flat_book_->mid_price_bps();
}
inline uint32_t BookRouter::qty_at(Side side, int64_t p) const noexcept {
  return flat_book_->qty_at(side, p);
}
inline std::vector<LevelSummary> BookRouter::depth(Side side,
                                                   int32_t levels) const {
  return flat_book_->depth(side, levels);
}

// ── Order lifecycle delegates ────────────────────────────────────────────────

inline std::optional<std::reference_wrapper<const Order>>
BookRouter::find_order(uint64_t id) const {
  return order_book_->find_order(id);
}

inline size_t BookRouter::bid_levels() const {
  return order_book_->bid_levels();
}
inline size_t BookRouter::ask_levels() const {
  return order_book_->ask_levels();
}
