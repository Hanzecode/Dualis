// ─────────────────────────────────────────────────────────────────────────────
//  QUANTCORE TESTS
//  These double as usage examples — read this file to understand how to use
//  every component. Uses a minimal hand-rolled test framework to avoid
//  introducing Catch2/GoogleTest as a dependency.
//
//  To compile (no ZMQ/nlohmann needed for these tests):
//    g++ -std=c++20 -I../include -o tests tests.cpp ../src/order_book.cpp
//    ./tests
// ─────────────────────────────────────────────────────────────────────────────

#include "order_book.hpp"
#include "risk_manager.hpp"

#include <iostream>    // std::cout — test output
#include <cassert>     // assert() — fails with abort() if condition is false
#include <vector>      // Collecting trades in test callbacks
#include <string>

// ─────────────────────────────────────────────────────────────────────────────
//  MINIMAL TEST HARNESS
// ─────────────────────────────────────────────────────────────────────────────

static int tests_passed = 0;
static int tests_failed = 0;

// Macro: captures file/line for useful failure messages
// #expr stringifies the expression — __FILE__ / __LINE__ are compiler built-ins
#define CHECK(expr)                                                         \
    do {                                                                    \
        if (!(expr)) {                                                      \
            std::cerr << "FAIL [" << __FILE__ << ":" << __LINE__ << "] "   \
                      << #expr << "\n";                                     \
            ++tests_failed;                                                 \
        } else {                                                            \
            ++tests_passed;                                                 \
        }                                                                   \
    } while (0)
// 'while(0)' makes the macro safe inside if/else without braces

#define TEST(name)                                                          \
    static void name();                                                     \
    struct _reg_##name { _reg_##name() { name(); } } _reg_inst_##name;     \
    static void name()
// TEST(foo) declares the function and registers it to run at static init time.
// This is the "self-registering test" pattern — no manual list of tests needed.

// ─────────────────────────────────────────────────────────────────────────────
//  HELPERS
// ─────────────────────────────────────────────────────────────────────────────

// Build a limit order with minimal boilerplate
Order make_limit_order(Side side, int64_t price_bps, uint32_t qty,
                       const std::string& symbol = "AAPL") {
    Order o;
    o.symbol     = symbol;
    o.side       = side;
    o.type       = OrderType::LIMIT;
    o.price_bps  = price_bps;
    o.quantity   = qty;
    o.filled_qty = 0;
    o.status     = OrderStatus::NEW;
    return o;
}

Order make_market_order(Side side, uint32_t qty, const std::string& symbol = "AAPL") {
    Order o;
    o.symbol     = symbol;
    o.side       = side;
    o.type       = OrderType::MARKET;
    o.price_bps  = 0;   // Market orders have no price
    o.quantity   = qty;
    o.filled_qty = 0;
    o.status     = OrderStatus::NEW;
    return o;
}

// ─────────────────────────────────────────────────────────────────────────────
//  ORDER BOOK TESTS
// ─────────────────────────────────────────────────────────────────────────────

TEST(test_empty_book_has_no_best_prices) {
    std::vector<Trade> trades;

    // Lambda captures 'trades' by reference — push each trade for assertion
    OrderBook book("AAPL", [&trades](const Trade& t) { trades.push_back(t); });

    // Empty book: both sides should return nullopt
    CHECK(!book.best_bid().has_value());   // has_value() = false means nullopt
    CHECK(!book.best_ask().has_value());
    CHECK(!book.spread_bps().has_value());
    CHECK(trades.empty());
}

TEST(test_limit_bid_rests_in_book) {
    std::vector<Trade> trades;
    OrderBook book("AAPL", [&trades](const Trade& t) { trades.push_back(t); });

    // Submit a buy limit that won't match (no asks in book)
    uint64_t id = book.submit(make_limit_order(Side::BUY, 15000, 100));
    // Price 15000 bps = $150.00

    CHECK(book.best_bid().has_value());
    CHECK(*book.best_bid() == 15000);   // Dereference optional with *
    CHECK(!book.best_ask().has_value());
    CHECK(book.bid_levels() == 1);
    CHECK(trades.empty());              // No match yet

    // The order should be findable by ID
    auto opt_order = book.find_order(id);
    CHECK(opt_order.has_value());
    CHECK(opt_order->get().status == OrderStatus::OPEN);
    CHECK(opt_order->get().remaining() == 100);
}

TEST(test_matching_limit_orders) {
    std::vector<Trade> trades;
    OrderBook book("AAPL", [&trades](const Trade& t) { trades.push_back(t); });

    // Place a sell limit first (rests in book as maker)
    uint64_t sell_id = book.submit(make_limit_order(Side::SELL, 15000, 100));
    CHECK(*book.best_ask() == 15000);
    CHECK(trades.empty());

    // Place a crossing buy limit — should match immediately
    uint64_t buy_id = book.submit(make_limit_order(Side::BUY, 15000, 100));

    // One trade should have fired
    CHECK(trades.size() == 1);
    CHECK(trades[0].price_bps == 15000);   // Trade at maker's price
    CHECK(trades[0].quantity  == 100);
    CHECK(trades[0].maker_order_id == sell_id);
    CHECK(trades[0].taker_order_id == buy_id);

    // Book should now be empty — both orders fully filled
    CHECK(!book.best_ask().has_value());
    CHECK(!book.best_bid().has_value());
    CHECK(book.ask_levels() == 0);
    CHECK(book.bid_levels() == 0);
}

TEST(test_partial_fill) {
    std::vector<Trade> trades;
    OrderBook book("AAPL", [&trades](const Trade& t) { trades.push_back(t); });

    // Maker: sell 100
    book.submit(make_limit_order(Side::SELL, 15000, 100));

    // Taker: buy 60 — partial fill
    uint64_t buy_id = book.submit(make_limit_order(Side::BUY, 15000, 60));

    CHECK(trades.size() == 1);
    CHECK(trades[0].quantity == 60);

    // Sell order should still have 40 remaining in the book
    CHECK(book.ask_levels() == 1);
    CHECK(*book.best_ask() == 15000);

    // Buy order was fully filled — should not be in book
    CHECK(!book.find_order(buy_id).has_value());   // Filled orders removed from lookup
}

TEST(test_best_bid_is_highest_price) {
    OrderBook book("AAPL", [](const Trade&) {});

    // Submit bids at three different prices
    book.submit(make_limit_order(Side::BUY, 14900, 100));  // $149.00
    book.submit(make_limit_order(Side::BUY, 15000, 100));  // $150.00 — should be best
    book.submit(make_limit_order(Side::BUY, 14800, 100));  // $148.00

    // Best bid = highest price — the descending BidMap ensures begin() = max
    CHECK(*book.best_bid() == 15000);
    CHECK(book.bid_levels() == 3);
}

TEST(test_best_ask_is_lowest_price) {
    OrderBook book("AAPL", [](const Trade&) {});

    book.submit(make_limit_order(Side::SELL, 15100, 100));  // $151.00
    book.submit(make_limit_order(Side::SELL, 15000, 100));  // $150.00 — should be best
    book.submit(make_limit_order(Side::SELL, 15200, 100));  // $152.00

    // Best ask = lowest price — ascending AskMap ensures begin() = min
    CHECK(*book.best_ask() == 15000);
}

TEST(test_spread_calculation) {
    OrderBook book("AAPL", [](const Trade&) {});

    book.submit(make_limit_order(Side::BUY,  14900, 100));  // best bid $149.00
    book.submit(make_limit_order(Side::SELL, 15100, 100));  // best ask $151.00

    // Spread = best_ask − best_bid = 15100 − 14900 = 200 bps ($2.00)
    CHECK(book.spread_bps().has_value());
    CHECK(*book.spread_bps() == 200);
    CHECK(*book.mid_price_bps() == 15000);   // (14900+15100)/2
}

TEST(test_cancel_order) {
    OrderBook book("AAPL", [](const Trade&) {});

    uint64_t id = book.submit(make_limit_order(Side::BUY, 15000, 100));
    CHECK(book.bid_levels() == 1);

    // Cancel it
    bool cancelled = book.cancel(id);
    CHECK(cancelled);
    CHECK(book.bid_levels() == 0);              // Level removed
    CHECK(!book.find_order(id).has_value());    // No longer in lookup
    CHECK(!book.best_bid().has_value());        // Empty book

    // Cancelling again should return false
    CHECK(!book.cancel(id));
}

TEST(test_market_order_sweeps_book) {
    std::vector<Trade> trades;
    OrderBook book("AAPL", [&trades](const Trade& t) { trades.push_back(t); });

    // Two resting sell limits at different prices
    book.submit(make_limit_order(Side::SELL, 15000, 50));   // 50 @ $150
    book.submit(make_limit_order(Side::SELL, 15100, 50));   // 50 @ $151

    // Market buy 80 — should fill 50 @ 15000, then 30 @ 15100
    book.submit(make_market_order(Side::BUY, 80));

    CHECK(trades.size() == 2);
    CHECK(trades[0].price_bps == 15000);  CHECK(trades[0].quantity == 50);
    CHECK(trades[1].price_bps == 15100);  CHECK(trades[1].quantity == 30);

    // 20 remaining at $151 level
    CHECK(book.ask_levels() == 1);
    CHECK(*book.best_ask() == 15100);
}

TEST(test_fifo_priority) {
    std::vector<Trade> trades;
    OrderBook book("AAPL", [&trades](const Trade& t) { trades.push_back(t); });

    // Two sellers at the same price — FIFO: first submitted fills first
    uint64_t first  = book.submit(make_limit_order(Side::SELL, 15000, 100));
    uint64_t second = book.submit(make_limit_order(Side::SELL, 15000, 100));

    // Taker buys only 100 — should match the FIRST seller
    book.submit(make_limit_order(Side::BUY, 15000, 100));

    CHECK(trades.size() == 1);
    CHECK(trades[0].maker_order_id == first);   // First order filled (FIFO)

    // Second order still in book
    CHECK(book.find_order(second).has_value());
}

// ─────────────────────────────────────────────────────────────────────────────
//  RISK MANAGER TESTS
// ─────────────────────────────────────────────────────────────────────────────

TEST(test_risk_approves_normal_order) {
    RiskLimits limits;
    limits.max_position_per_symbol  = 10000;
    limits.max_total_notional_bps   = 100000000;
    limits.max_order_size           = 1000;
    limits.max_orders_per_second    = 100;
    limits.max_loss_bps             = 10000000;
    limits.max_concentration        = 0.5;

    RiskManager risk(limits);

    Order order = make_limit_order(Side::BUY, 15000, 100);
    auto result = risk.check(order);
    CHECK(result.approved());
}

TEST(test_risk_rejects_oversized_order) {
    RiskLimits limits{};
    limits.max_order_size           = 50;     // Very small limit for testing
    limits.max_position_per_symbol  = 10000;
    limits.max_orders_per_second    = 100;
    limits.max_loss_bps             = 999999999;

    RiskManager risk(limits);

    Order order = make_limit_order(Side::BUY, 15000, 100);  // 100 > 50 limit
    auto result = risk.check(order);

    CHECK(!result.approved());
    CHECK(result.reason == RiskManager::RejectionReason::ORDER_SIZE);
}

// ─────────────────────────────────────────────────────────────────────────────
//  MAIN
// ─────────────────────────────────────────────────────────────────────────────

int main() {
    // All TEST() blocks ran at static initialisation time (before main)
    // We just print the summary here

    std::cout << "\n──────────────────────────────────\n";
    std::cout << "  QuantCore C++ Test Results\n";
    std::cout << "──────────────────────────────────\n";
    std::cout << "  Passed: " << tests_passed << "\n";
    std::cout << "  Failed: " << tests_failed << "\n";
    std::cout << "──────────────────────────────────\n\n";

    // Return non-zero exit code on failure — CI systems check this
    return tests_failed > 0 ? 1 : 0;
}
