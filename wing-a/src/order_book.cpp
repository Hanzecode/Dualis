#include "order_book.hpp"

#include <algorithm> // std::min — used to compute fill quantity
#include <cassert>   // assert() — cheap runtime checks in debug builds
#include <stdexcept> // std::invalid_argument, std::runtime_error

// ─────────────────────────────────────────────────────────────────────────────
//  CONSTRUCTOR
//  Member initialiser list (: symbol_(...), ...) is used instead of
//  assignments in the body. Why? Members are constructed once in the list,
//  vs. default-constructed then assigned in the body — wasteful for strings.
// ─────────────────────────────────────────────────────────────────────────────

OrderBook::OrderBook(std::string symbol, TradeCallback on_trade)
    : symbol_(
          std::move(symbol)) // std::move: transfers ownership of the string's
                             // heap buffer; 'symbol' becomes empty after this.
                             // Without move: we'd copy the entire string.
      ,
      bids_{} // Value-initialises the BidMap to empty
      ,
      asks_{}, orders_{},
      on_trade_(std::move(on_trade)) // Move the callback functor — same reason
      ,
      next_order_id_(0), next_trade_id_(0) {
  // Validate symbol is not empty — fail loudly at construction, not silently
  // later
  if (symbol_.empty()) {
    throw std::invalid_argument("OrderBook: symbol must not be empty");
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  SUBMIT
//  Assigns an ID, timestamps the order, routes to the matcher.
//  Returns the assigned order ID so the caller can track/cancel it.
// ─────────────────────────────────────────────────────────────────────────────

uint64_t OrderBook::submit(Order order) {
  // Validate before touching any state — throw before mutating
  if (order.quantity == 0) {
    throw std::invalid_argument("OrderBook::submit: quantity must be > 0");
  }
  if (order.type == OrderType::LIMIT && order.price_bps <= 0) {
    throw std::invalid_argument(
        "OrderBook::submit: limit price must be positive");
  }

  // Assign identity and timestamp
  order.order_id = next_order_id();
  order.filled_qty = 0; // Nothing filled yet
  order.status = OrderStatus::NEW;
  order.timestamp =
      std::chrono::system_clock::now(); // Wall-clock — correct for audit logs

  uint64_t id = order.order_id; // Capture before we move the order into the map

  // Insert into the flat lookup map first.
  // orders_.emplace(key, value): constructs in-place — no copy of Order.
  // emplace returns pair<iterator, bool>; we ignore the iterator here.
  auto [it, inserted] = orders_.emplace(id, std::move(order));

  if (!inserted) {
    throw std::runtime_error("OrderBook::submit: duplicate order_id generated");
  }

  // Get a reference because we moved 'order' above, can't use it
  Order &stored = it->second; // This will take the value (Order) from the map
  stored.status = OrderStatus::OPEN;

  // Trying to fill with whatever resting in the book — may result in immediate
  // full or partial fill
  match(stored);

  // If limit order has remaining quantity after matching, add to book and
  // become resting
  if (stored.type == OrderType::LIMIT && stored.remaining() > 0) {
    add_to_book(stored);
  }

  // Market orders never rest in the book — if unfilled, they're cancelled
  if (stored.type == OrderType::MARKET && stored.remaining() > 0) {
    stored.status = OrderStatus::CANCELLED;
  }

  // Fully filled taker orders don't rest in the book — remove from lookup map
  // Resting makers are removed from orders_ inside execute_fill when they
  // complete
  if (stored.status == OrderStatus::FILLED) {
    orders_.erase(id); // Iterator 'it' is now invalid; use id for erase
  }

  return id;
}

// ─────────────────────────────────────────────────────────────────────────────
//  CANCEL
//  Removes an order from the price level queue and the flat lookup map.
//  Returns false (not an exception) if the order is missing — callers often
//  cancel speculatively and shouldn't have to try/catch.
// ─────────────────────────────────────────────────────────────────────────────

bool OrderBook::cancel(uint64_t order_id) {
  // find() returns end() iterator if key not found — never throws
  auto it = orders_.find(order_id);
  if (it == orders_.end()) {
    return false; // Order doesn't exist (already filled, cancelled, or bad ID)
  }

  Order &order = it->second; // This will pass the key to the order

  // Only OPEN or PARTIALLY_FILLED orders can be cancelled
  if (order.status == OrderStatus::FILLED ||
      order.status == OrderStatus::CANCELLED) {
    return false;
  }

  order.status = OrderStatus::CANCELLED;

  // Remove from the price-level queue on the correct side
  // NOTE: std::queue has no random removal — we reconstruct it without this ID.
  // In production with high cancel rates, consider std::list + iterator
  // tracking.
  auto remove_from_side = [&](auto &side_map) {
    // Lambda captures by reference [&] — can access 'order' and 'order_id'
    auto level_it = side_map.find(order.price_bps);
    if (level_it == side_map.end())
      return;

    PriceLevel &level = level_it->second;

    // Rebuild the queue without the cancelled order ID
    // This is O(n) in orders at this level — acceptable for low cancel rates
    std::queue<uint64_t> rebuilt;
    while (!level.order_ids.empty()) {
      uint64_t front_id = level.order_ids.front();
      level.order_ids.pop(); // Remove from front
      if (front_id != order_id) {
        rebuilt.push(front_id); // Keep everything else
      }
    }
    level.order_ids = std::move(rebuilt);      // Replace with rebuilt queue
    level.total_quantity -= order.remaining(); // Adjust aggregate quantity

    // If the level is now empty, remove it from the map entirely
    if (level.order_ids.empty()) {
      side_map.erase(level_it);
    }
  };

  if (order.side == Side::BUY) {
    remove_from_side(bids_);
  } else {
    remove_from_side(asks_);
  }

  // Remove from flat lookup — erase by iterator is O(1)
  orders_.erase(it);
  return true;
}

// ─────────────────────────────────────────────────────────────────────────────
//  MATCH (dispatcher)
//  Routes to the appropriate matching strategy based on order type.
// ─────────────────────────────────────────────────────────────────────────────

void OrderBook::match(Order &incoming) {
  if (incoming.type == OrderType::LIMIT) {
    match_limit(incoming);
  } else {
    match_market(incoming);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  MATCH_LIMIT
//  A BUY limit crosses an ASK if: buy_price >= ask_price
//  A SELL limit crosses a BID if: sell_price <= bid_price
//
//  We iterate from the best (front) of the opposite side's map.
//  For BUY: asks_ is sorted ascending, begin() = lowest ask = best ask
//  For SELL: bids_ is sorted descending, begin() = highest bid = best bid
// ─────────────────────────────────────────────────────────────────────────────

void OrderBook::match_limit(Order &incoming) {
  while (incoming.remaining() > 0) {
    if (incoming.side == Side::BUY) {
      if (asks_.empty())
        break; // No asks to match against

      auto best_ask_it = asks_.begin(); // Iterator to best (lowest) ask
      int64_t best_ask_price = best_ask_it->first;

      // Limit buy crosses if our price >= the ask price
      if (incoming.price_bps < best_ask_price)
        break; // No more crossable levels

      PriceLevel &level = best_ask_it->second;

      // Process orders at this level FIFO until either side runs out
      while (incoming.remaining() > 0 && !level.order_ids.empty()) {
        uint64_t maker_id =
            level.order_ids.front(); // Oldest order at this level
        Order &maker =
            orders_.at(maker_id); // at() throws if missing (defensive)

        uint32_t fill_qty = std::min(incoming.remaining(), maker.remaining());
        execute_fill(maker, incoming,
                     fill_qty); // Updates both orders, fires callback

        if (maker.remaining() == 0) {
          // Maker fully filled — remove from level queue and lookup map
          level.order_ids.pop();
          orders_.erase(maker_id);
        }
      }

      level.total_quantity = 0;
      // Recompute total_quantity by scanning remaining orders at this level
      // (Could maintain incrementally — left explicit for clarity)
      {
        std::queue<uint64_t> temp = level.order_ids; // Copy for scanning
        while (!temp.empty()) {
          level.total_quantity += orders_.at(temp.front()).remaining();
          temp.pop();
        }
      }

      if (level.order_ids.empty()) {
        asks_.erase(best_ask_it); // Remove exhausted level
      }

    } else { // SELL side — mirror logic
      // Note: fall-through to symmetric SELL block below
      if (bids_.empty())
        break;

      auto best_bid_it = bids_.begin();
      int64_t best_bid_price = best_bid_it->first;

      if (incoming.price_bps > best_bid_price)
        break; // Sell price too high to cross

      PriceLevel &level = best_bid_it->second;

      while (incoming.remaining() > 0 && !level.order_ids.empty()) {
        uint64_t maker_id = level.order_ids.front();
        Order &maker = orders_.at(maker_id);

        uint32_t fill_qty = std::min(incoming.remaining(), maker.remaining());
        execute_fill(maker, incoming, fill_qty);

        if (maker.remaining() == 0) {
          level.order_ids.pop();
          orders_.erase(maker_id);
        }
      }

      level.total_quantity = 0;
      {
        std::queue<uint64_t> temp = level.order_ids;
        while (!temp.empty()) {
          level.total_quantity += orders_.at(temp.front()).remaining();
          temp.pop();
        }
      }

      if (level.order_ids.empty()) {
        bids_.erase(best_bid_it);
      }
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  MATCH_MARKET
//  Market orders consume as much liquidity as available.
//  They never rest in the book — if not filled, they are cancelled.
//  Price check is skipped; we match at whatever the best available price is.
// ─────────────────────────────────────────────────────────────────────────────

void OrderBook::match_market(Order &incoming) {
  while (incoming.remaining() > 0) {
    if (incoming.side == Side::BUY) {
      if (asks_.empty())
        break; // No liquidity — remaining will be cancelled

      auto level_it = asks_.begin();
      PriceLevel &level = level_it->second;

      while (incoming.remaining() > 0 && !level.order_ids.empty()) {
        uint64_t maker_id = level.order_ids.front();
        Order &maker = orders_.at(maker_id);

        // For market orders, we fill at the maker's price (incoming.price_bps
        // is ignored — set execute_fill to use maker's price)
        uint32_t fill_qty = std::min(incoming.remaining(), maker.remaining());
        incoming.price_bps =
            maker.price_bps; // Market order takes maker's price
        execute_fill(maker, incoming, fill_qty);

        if (maker.remaining() == 0) {
          level.order_ids.pop();
          orders_.erase(maker_id);
        }
      }

      level.total_quantity = 0;
      {
        std::queue<uint64_t> temp = level.order_ids;
        while (!temp.empty()) {
          level.total_quantity += orders_.at(temp.front()).remaining();
          temp.pop();
        }
      }

      if (level.order_ids.empty()) {
        asks_.erase(level_it);
      }

    } else {
      if (bids_.empty())
        break;

      auto level_it = bids_.begin();
      PriceLevel &level = level_it->second;

      while (incoming.remaining() > 0 && !level.order_ids.empty()) {
        uint64_t maker_id = level.order_ids.front();
        Order &maker = orders_.at(maker_id);

        uint32_t fill_qty = std::min(incoming.remaining(), maker.remaining());
        incoming.price_bps = maker.price_bps;
        execute_fill(maker, incoming, fill_qty);

        if (maker.remaining() == 0) {
          level.order_ids.pop();
          orders_.erase(maker_id);
        }
      }

      level.total_quantity = 0;
      {
        std::queue<uint64_t> temp = level.order_ids;
        while (!temp.empty()) {
          level.total_quantity += orders_.at(temp.front()).remaining();
          temp.pop();
        }
      }

      if (level.order_ids.empty()) {
        bids_.erase(level_it);
      }
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  EXECUTE_FILL
//  The single point where a trade is recorded.
//  Both maker and taker orders are updated; the on_trade_ callback is fired.
//  This is where risk checks, PnL tracking, and audit logs would hook in.
// ─────────────────────────────────────────────────────────────────────────────

void OrderBook::execute_fill(Order &maker, Order &taker, uint32_t fill_qty) {
  assert(fill_qty > 0); // A zero fill is a bug, not a valid state
  assert(fill_qty <= maker.remaining());
  assert(fill_qty <= taker.remaining());

  // Update fill quantities
  maker.filled_qty += fill_qty;
  taker.filled_qty += fill_qty;

  // Update statuses — partial fill keeps order OPEN; full fill closes it
  auto update_status = [](Order &o) {
    if (o.remaining() == 0) {
      o.status = OrderStatus::FILLED;
    } else {
      o.status = OrderStatus::PARTIALLY_FILLED;
    }
  };
  update_status(maker);
  update_status(taker);

  // Build trade record — trades always execute at the MAKER's price
  // taker_side is now carried in the Trade struct directly (fixes the
  // pending_taker_sides_ sentinel race in ExecutionEngine — taker.side is
  // available here at fill time)
  Trade trade{
      .trade_id = next_trade_id(),
      .maker_order_id = maker.order_id,
      .taker_order_id = taker.order_id,
      .taker_side = taker.side, // FIX: record taker side at the fill point
      .our_side = taker.is_synthetic ? maker.side : taker.side,
      .symbol = symbol_,
      .price_bps = maker.price_bps,
      .quantity = fill_qty,
      .timestamp =
          std::chrono::system_clock::now() // FIX: system_clock = Unix epoch
  };
  // Designated initialisers (C++20): struct fields named explicitly.
  // Safer than positional initialisation — adding a field won't silently shift
  // values.

  // Fire the callback — downstream consumers (PnL, risk, ZMQ publisher) receive
  // it
  if (on_trade_) { // Check callback is set (std::function is null-checkable)
    on_trade_(trade);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  ADD_TO_BOOK
//  Places a resting limit order into the appropriate price level.
//  Creates the level if it doesn't exist yet.
// ─────────────────────────────────────────────────────────────────────────────

void OrderBook::add_to_book(const Order &order) {
  auto add_to_side = [&](auto &side_map) {
    // operator[] creates a default-constructed PriceLevel if key absent.
    // This is intentional — it's the "find or create" pattern.
    PriceLevel &level = side_map[order.price_bps];
    level.price_bps = order.price_bps;
    level.order_ids.push(order.order_id);      // Enqueue at back (FIFO)
    level.total_quantity += order.remaining(); // Update aggregate
  };

  if (order.side == Side::BUY) {
    add_to_side(bids_);
  } else {
    add_to_side(asks_);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  QUERY METHODS
// ─────────────────────────────────────────────────────────────────────────────

std::optional<int64_t> OrderBook::best_bid() const {
  if (bids_.empty())
    return std::nullopt;       // std::nullopt = the "no value" sentinel
  return bids_.begin()->first; // begin() = highest price (descending map)
}

std::optional<int64_t> OrderBook::best_ask() const {
  if (asks_.empty())
    return std::nullopt;
  return asks_.begin()->first; // begin() = lowest price (ascending map)
}

std::optional<int64_t> OrderBook::spread_bps() const {
  auto bid = best_bid();
  auto ask = best_ask();
  if (!bid || !ask)
    return std::nullopt; // Can't compute spread with one side empty
  return *ask - *bid;    // Dereference optional with * to get the value
}

std::optional<int64_t> OrderBook::mid_price_bps() const {
  auto bid = best_bid();
  auto ask = best_ask();
  if (!bid || !ask)
    return std::nullopt;
  return (*bid + *ask) / 2;
}

std::optional<std::reference_wrapper<const Order>>
OrderBook::find_order(uint64_t order_id) const {
  auto it = orders_.find(order_id);
  if (it == orders_.end())
    return std::nullopt;
  // std::reference_wrapper<const Order> wraps a reference in an
  // optional-compatible type (std::optional can't hold plain references —
  // they're not copyable/assignable)
  return std::cref(it->second);
}