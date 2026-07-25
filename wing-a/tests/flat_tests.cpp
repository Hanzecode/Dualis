// ─────────────────────────────────────────────────────────────────────────────
//  FLAT ORDER BOOK + BOOK ROUTER TESTS
//  Compile: g++ -std=c++20 -I../include -o flat_tests flat_tests.cpp
//  ../src/order_book.cpp
// ─────────────────────────────────────────────────────────────────────────────

#include "book_router.hpp"     // BookRouter
#include "flat_order_book.hpp" // FlatOrderBook
#include "order_book.hpp"      // Order, Side, Trade helpers

#include <cassert>
#include <iostream>
#include <vector>

// ── Minimal test harness (same pattern as tests.cpp) ────────────────────────

static int tests_passed = 0;
static int tests_failed = 0;

#define CHECK(expr)                                                            \
  do {                                                                         \
    if (!(expr)) {                                                             \
      std::cerr << "FAIL [" << __FILE__ << ":" << __LINE__ << "] " << #expr    \
                << "\n";                                                       \
      ++tests_failed;                                                          \
    } else {                                                                   \
      ++tests_passed;                                                          \
    }                                                                          \
  } while (0)

#define TEST(name)                                                             \
  static void name();                                                          \
  struct _reg_##name {                                                         \
    _reg_##name() { name(); }                                                  \
  } _reg_inst_##name;                                                          \
  static void name()

// ── Helper ───────────────────────────────────────────────────────────────────

Order make_limit(Side side, int64_t price_bps, uint32_t qty,
                 const std::string &sym = "AAPL") {
  Order o;
  o.symbol = sym;
  o.side = side;
  o.type = OrderType::LIMIT;
  o.price_bps = price_bps;
  o.quantity = qty;
  o.filled_qty = 0;
  o.status = OrderStatus::NEW;
  return o;
}

// ─────────────────────────────────────────────────────────────────────────────
//  FLAT ORDER BOOK TESTS
// ─────────────────────────────────────────────────────────────────────────────

// Base price for all tests: 15000 bps ($150.00 if 1 bps = $0.01)
// Window: [15000 - 10000, 15000 + 10000] = [5000, 25000]
static constexpr int64_t BASE = 15000;

TEST(flat_empty_book_has_no_best_prices) {
  FlatOrderBook book("AAPL", BASE);

  // Fresh book: both sides empty → optionals are nullopt
  CHECK(!book.best_bid().has_value());
  CHECK(!book.best_ask().has_value());
  CHECK(!book.spread_bps().has_value());
  CHECK(!book.mid_price_bps().has_value());
}

TEST(flat_add_bid_visible_as_best_bid) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::BUY, 14900, 100); // Add 100 shares at $149.00

  CHECK(book.best_bid().has_value());
  CHECK(*book.best_bid() == 14900); // Only one bid — must be best
  CHECK(book.qty_at(Side::BUY, 14900) ==
        100);                          // Verify quantity stored correctly
  CHECK(!book.best_ask().has_value()); // Ask side still empty
}

TEST(flat_add_ask_visible_as_best_ask) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::SELL, 15100, 200);

  CHECK(book.best_ask().has_value());
  CHECK(*book.best_ask() == 15100);
  CHECK(book.qty_at(Side::SELL, 15100) == 200);
}

TEST(flat_best_bid_is_highest_price) {
  FlatOrderBook book("AAPL", BASE);

  // Add three bid levels — best should be the highest
  book.add_quantity(Side::BUY, 14800, 100); // $148.00
  book.add_quantity(Side::BUY, 15000, 100); // $150.00 ← should be best
  book.add_quantity(Side::BUY, 14900, 100); // $149.00

  CHECK(*book.best_bid() == 15000);
}

TEST(flat_best_ask_is_lowest_price) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::SELL, 15200, 100);
  book.add_quantity(Side::SELL, 15000, 100); // $150.00 ← should be best
  book.add_quantity(Side::SELL, 15100, 100);

  CHECK(*book.best_ask() == 15000);
}

TEST(flat_spread_and_mid_correct) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::BUY, 14900, 100);  // best bid $149.00
  book.add_quantity(Side::SELL, 15100, 100); // best ask $151.00

  // spread = 15100 - 14900 = 200 bps
  CHECK(*book.spread_bps() == 200);
  // mid = (14900 + 15100) / 2 = 15000
  CHECK(*book.mid_price_bps() == 15000);
}

TEST(flat_remove_quantity_partial) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::BUY, 15000, 100);
  book.remove_quantity(Side::BUY, 15000, 40); // Remove 40 of 100

  CHECK(book.qty_at(Side::BUY, 15000) == 60); // 60 remaining
  CHECK(*book.best_bid() == 15000);           // Level still exists
}

TEST(flat_remove_quantity_clears_level) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::BUY, 15000, 100);
  book.remove_quantity(Side::BUY, 15000, 100); // Remove all

  CHECK(book.qty_at(Side::BUY, 15000) == 0);
  // best_bid cache should be stale — scanning should find NO_PRICE
  CHECK(!book.best_bid().has_value());
}

TEST(flat_remove_quantity_clamps_to_zero) {
  // Removing more than available should not underflow (unsigned wraparound =
  // disaster)
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::BUY, 15000, 50);
  book.remove_quantity(Side::BUY, 15000, 200); // 200 > 50 — should clamp to 0

  CHECK(book.qty_at(Side::BUY, 15000) ==
        0); // Clamped, not wrapped to huge number
  CHECK(!book.best_bid().has_value());
}

TEST(flat_set_quantity_overwrites) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::SELL, 15000, 100);
  book.set_quantity(Side::SELL, 15000,
                    75); // Snapshot update: overwrite with 75

  CHECK(book.qty_at(Side::SELL, 15000) == 75);
}

TEST(flat_out_of_range_price_throws) {
  FlatOrderBook book("AAPL", BASE); // Window: [5000, 25000]

  bool threw = false;
  try {
    // With BASE=15000 and HALF_RANGE=200000, the window spans:
    // [15000 - 200000, 15000 + 200000] = [-185000, 215000]
    // Price -200000 maps to slot = (-200000 - 15000) + 200000 = -15000 → out of
    // range
    book.add_quantity(Side::BUY, -200000, 100); // below window floor
  } catch (const std::out_of_range &) {
    threw = true;
  }
  CHECK(threw);
}

TEST(flat_qty_at_out_of_range_returns_zero) {
  // qty_at() is noexcept — returns 0 for out-of-range prices instead of
  // throwing
  FlatOrderBook book("AAPL", BASE);

  uint32_t qty = book.qty_at(Side::BUY, 1000); // Outside window
  CHECK(qty == 0);                             // Safe return, no throw
}

TEST(flat_total_qty) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::BUY, 14900, 100);
  book.add_quantity(Side::BUY, 14800, 200);
  book.add_quantity(Side::BUY, 14700, 50);

  CHECK(book.total_qty(Side::BUY) == 350);
  CHECK(book.total_qty(Side::SELL) == 0);
}

TEST(flat_depth_returns_levels_best_first) {
  FlatOrderBook book("AAPL", BASE);

  // Add 3 bid levels
  book.add_quantity(Side::BUY, 14700, 300);
  book.add_quantity(Side::BUY, 14800, 200);
  book.add_quantity(Side::BUY, 14900, 100); // best

  auto d = book.depth(Side::BUY, 3);

  CHECK(d.size() == 3);
  CHECK(d[0].price_bps == 14900); // Best bid first (highest)
  CHECK(d[0].total_quantity == 100);
  CHECK(d[1].price_bps == 14800);
  CHECK(d[2].price_bps == 14700);
  CHECK(d[2].total_quantity == 300);
}

TEST(flat_depth_asks_best_first) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::SELL, 15100, 100);
  book.add_quantity(Side::SELL, 15200, 200);
  book.add_quantity(Side::SELL, 15000, 50); // best ask

  auto d = book.depth(Side::SELL, 3);

  CHECK(d.size() == 3);
  CHECK(d[0].price_bps == 15000); // Best ask first (lowest)
  CHECK(d[1].price_bps == 15100);
  CHECK(d[2].price_bps == 15200);
}

TEST(flat_recenter_shifts_window) {
  FlatOrderBook book("AAPL", BASE); // window centred at 15000

  // Add qty near old centre
  book.add_quantity(Side::BUY, 14900, 100);

  // Recenter to 20000 (market moved up by 5000 bps)
  book.recenter(20000);

  // Old price 14900 is now relative to new centre 20000
  // Old slot for 14900 was: 14900 - 15000 + 10000 = 9900
  // After shift of 5000: new slot = 9900 - 5000 = 4900 → new price = 20000 +
  // (4900 - 10000) = 14900 So 14900 is still in range (within ±10000 of new
  // centre 20000) New window: [10000, 30000]
  CHECK(book.in_range(14900));
  CHECK(book.qty_at(Side::BUY, 14900) == 100); // Quantity preserved

  // Old centre 15000 is now in range of new window [10000, 30000]
  CHECK(book.in_range(15000));

  // New window after recenter to 20000: [20000-200000, 20000+200000] =
  // [-180000, 220000] Price 5000 IS now in range — use a truly out-of-range
  // price instead
  CHECK(!book.in_range(-500000)); // Far below any valid window
}

TEST(flat_clear_resets_everything) {
  FlatOrderBook book("AAPL", BASE);

  book.add_quantity(Side::BUY, 14900, 100);
  book.add_quantity(Side::SELL, 15100, 200);
  book.clear();

  CHECK(!book.best_bid().has_value());
  CHECK(!book.best_ask().has_value());
  CHECK(book.total_qty(Side::BUY) == 0);
  CHECK(book.total_qty(Side::SELL) == 0);
}

TEST(flat_add_multiple_qty_to_same_level) {
  FlatOrderBook book("AAPL", BASE);

  // Simulate two orders resting at the same price
  book.add_quantity(Side::BUY, 15000, 100); // First order
  book.add_quantity(Side::BUY, 15000, 150); // Second order at same price

  CHECK(book.qty_at(Side::BUY, 15000) == 250); // Quantities should accumulate
}

// ─────────────────────────────────────────────────────────────────────────────
//  BOOK ROUTER TESTS
// ─────────────────────────────────────────────────────────────────────────────

TEST(router_submit_resting_order_appears_in_flat_book) {
  std::vector<Trade> trades;
  BookRouter router("AAPL", BASE, [&](const Trade &t) { trades.push_back(t); });

  // Submit a buy limit that won't match (no asks in either book)
  router.submit(make_limit(Side::BUY, 14900, 100));

  // FlatOrderBook should reflect this resting order's quantity
  CHECK(router.qty_at(Side::BUY, 14900) == 100);

  // Price discovery via flat book
  CHECK(router.best_bid().has_value());
  CHECK(*router.best_bid() == 14900);

  // OrderBook should show it as OPEN
  CHECK(trades.empty());
}

TEST(router_matched_orders_remove_qty_from_flat_book) {
  std::vector<Trade> trades;
  BookRouter router("AAPL", BASE, [&](const Trade &t) { trades.push_back(t); });

  // Resting sell order
  router.submit(make_limit(Side::SELL, 15000, 100));
  CHECK(router.qty_at(Side::SELL, 15000) == 100);

  // Crossing buy — should match and remove qty from flat book
  router.submit(make_limit(Side::BUY, 15000, 100));

  // Both sides should now be empty in the flat book
  CHECK(router.qty_at(Side::SELL, 15000) == 0);
  CHECK(!router.best_ask().has_value());
  CHECK(!router.best_bid().has_value());

  // Trade callback should have fired once
  CHECK(trades.size() == 1);
  CHECK(trades[0].price_bps == 15000);
  CHECK(trades[0].quantity == 100);
}

TEST(router_cancel_removes_qty_from_flat_book) {
  BookRouter router("AAPL", BASE, [](const Trade &) {});

  uint64_t id = router.submit(make_limit(Side::BUY, 14900, 100));
  CHECK(router.qty_at(Side::BUY, 14900) == 100);

  bool cancelled = router.cancel(id);
  CHECK(cancelled);
  CHECK(router.qty_at(Side::BUY, 14900) == 0);
  CHECK(!router.best_bid().has_value());
}

TEST(router_market_data_update_visible_in_flat_book) {
  BookRouter router("AAPL", BASE, [](const Trade &) {});

  // Simulate receiving an L2 depth update from the exchange feed
  // (not our own order — represents external market participants)
  router.on_market_depth(Side::SELL, 15050, 500);
  router.on_market_depth(Side::BUY, 14950, 300);

  CHECK(*router.best_ask() == 15050);
  CHECK(*router.best_bid() == 14950);
  CHECK(router.qty_at(Side::SELL, 15050) == 500);
  CHECK(router.qty_at(Side::BUY, 14950) == 300);

  // Spread = 15050 - 14950 = 100 bps
  CHECK(*router.spread_bps() == 100);
}

TEST(router_depth_query_returns_combined_depth) {
  BookRouter router("AAPL", BASE, [](const Trade &) {});

  // Add depth via market data feed (external liquidity)
  router.on_market_depth(Side::BUY, 14900, 500);
  router.on_market_depth(Side::BUY, 14800, 300);

  // Also add our own resting order
  router.submit(make_limit(Side::BUY, 14950, 100));

  auto d = router.depth(Side::BUY, 3);

  CHECK(d.size() == 3);
  CHECK(d[0].price_bps == 14950); // Our resting order at best bid
  CHECK(d[0].total_quantity == 100);
  CHECK(d[1].price_bps == 14900);
  CHECK(d[2].price_bps == 14800);
}

TEST(router_price_discovery_is_fast_path) {
  // Verify that best_bid/ask/spread come from FlatOrderBook, not OrderBook.
  // The OrderBook has no bids — only the flat book has market data.
  BookRouter router("AAPL", BASE, [](const Trade &) {});

  // Market data only (no orders submitted to order book)
  router.on_market_depth(Side::BUY, 14990, 1000);
  router.on_market_depth(Side::SELL, 15010, 1000);

  // These queries should hit FlatOrderBook — verified by checking spread
  CHECK(*router.spread_bps() == 20); // 15010 - 14990 = 20 bps
  CHECK(*router.mid_price_bps() == 15000);

  // OrderBook side should show nothing (no orders submitted)
  CHECK(router.bid_levels() == 0);
  CHECK(router.ask_levels() == 0);
}

// ─────────────────────────────────────────────────────────────────────────────
//  MAIN
// ─────────────────────────────────────────────────────────────────────────────

int main() {
  std::cout << "\n──────────────────────────────────\n";
  std::cout << "  FlatOrderBook + BookRouter Tests\n";
  std::cout << "──────────────────────────────────\n";
  std::cout << "  Passed: " << tests_passed << "\n";
  std::cout << "  Failed: " << tests_failed << "\n";
  std::cout << "──────────────────────────────────\n\n";
  return tests_failed > 0 ? 1 : 0;
}