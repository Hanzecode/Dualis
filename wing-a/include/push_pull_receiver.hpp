#pragma once

// ─────────────────────────────────────────────────────────────────────────────
//  PUSH/PULL ORDER RECEIVER
//
//  Your architecture diagram shows TWO ZMQ patterns:
//
//  PUB/SUB  (zmq_signal_consumer.hpp):
//    Python alpha engine → PUB → C++ SUB
//    Purpose: broadcast alpha signals to any subscriber (1-to-many)
//    Direction: one-way, fire-and-forget
//
//  PUSH/PULL  (this file):
//    Python order manager → PUSH → C++ PULL
//    Purpose: send individual orders with guaranteed delivery (1-to-1)
//    Direction: one-way, but load-balancing across multiple C++ workers
//
//  WHY PUSH/PULL INSTEAD OF PUB/SUB FOR ORDERS?
//  - PUB/SUB drops messages if no subscriber is connected yet (no buffering)
//  - PUSH/PULL queues messages in the socket until the PULL side connects
//  - Orders must not be dropped — PUSH/PULL is the correct pattern
//  - PUSH also load-balances across multiple PULL workers automatically
//    (useful if you scale to multiple execution engine instances)
//
//  MESSAGE FORMAT (same JSON as AlphaSignal but with explicit order fields):
//  {"type":"ORDER","symbol":"AAPL","side":"BUY","order_type":"LIMIT",
//   "price_bps":15050,"quantity":100}
//
//  {"type":"CANCEL","symbol":"AAPL","order_id":42}
//
//  {"type":"MARKET_DEPTH","symbol":"AAPL","side":"BID","price_bps":15000,"qty":500}
// ─────────────────────────────────────────────────────────────────────────────

#include <zmq.hpp>
#include <nlohmann/json.hpp>
#include <thread>
#include <atomic>
#include <functional>
#include <string>
#include <optional>
#include <stdexcept>

#include "order_book.hpp"    // Order, Side, OrderType


// ─────────────────────────────────────────────────────────────────────────────
//  INCOMING MESSAGE VARIANTS
//  Using a tagged union approach with an enum discriminator — cleaner than
//  having three separate callback types, easier to extend.
// ─────────────────────────────────────────────────────────────────────────────

enum class IncomingMessageType {
    ORDER,         // New order submission from Python
    CANCEL,        // Cancel an existing order
    MARKET_DEPTH,  // L2 depth update for FlatOrderBook
    UNKNOWN        // Malformed or unrecognised message — logged and dropped
};

struct IncomingOrder {
    Order order;   // Fully constructed Order ready for submission
};

struct IncomingCancel {
    std::string symbol;
    uint64_t    order_id;
};

struct IncomingMarketDepth {
    std::string symbol;
    Side        side;
    int64_t     price_bps;
    uint32_t    qty;
};

// Tagged union — holds exactly one of the three message types
struct IncomingMessage {
    IncomingMessageType type = IncomingMessageType::UNKNOWN;

    // std::optional for each variant — only one will have a value.
    // In C++17 you'd use std::variant<IncomingOrder, IncomingCancel, ...>;
    // using optional here keeps it explicit and easy to read.
    std::optional<IncomingOrder>      order;
    std::optional<IncomingCancel>     cancel;
    std::optional<IncomingMarketDepth> depth;
};


// ─────────────────────────────────────────────────────────────────────────────
//  PUSH/PULL RECEIVER CLASS
// ─────────────────────────────────────────────────────────────────────────────

class PushPullReceiver {
public:
    // Callback fired for each successfully parsed incoming message
    using MessageCallback = std::function<void(IncomingMessage)>;

    // endpoint example: "tcp://localhost:5557"
    // The Python PUSH socket connects TO this endpoint;
    // we BIND (listen) and PULL (receive).
    PushPullReceiver(std::string endpoint, MessageCallback on_message);
    ~PushPullReceiver() noexcept;

    void start();
    void stop() noexcept;

    [[nodiscard]] bool running() const { return running_.load(); }

private:
    std::string     endpoint_;
    MessageCallback on_message_;

    zmq::context_t  context_;
    std::unique_ptr<zmq::socket_t> socket_;   // Created on worker thread

    std::thread      worker_;
    std::atomic<bool> running_;
    std::atomic<bool> stop_requested_;

    void receive_loop();

    // Parse raw JSON string into an IncomingMessage
    [[nodiscard]] IncomingMessage parse(const std::string& raw) const;

    // Helpers to parse each message type from a JSON object
    [[nodiscard]] std::optional<IncomingOrder>
    parse_order(const nlohmann::json& j) const;

    [[nodiscard]] std::optional<IncomingCancel>
    parse_cancel(const nlohmann::json& j) const;

    [[nodiscard]] std::optional<IncomingMarketDepth>
    parse_depth(const nlohmann::json& j) const;
};


// ─────────────────────────────────────────────────────────────────────────────
//  IMPLEMENTATION
// ─────────────────────────────────────────────────────────────────────────────

inline PushPullReceiver::PushPullReceiver(std::string endpoint, MessageCallback on_message)
    : endpoint_(std::move(endpoint))
    , on_message_(std::move(on_message))
    , context_(1)                   // 1 I/O thread in the ZMQ context
    , running_(false)
    , stop_requested_(false)
{}

inline PushPullReceiver::~PushPullReceiver() noexcept {
    stop();   // Always join before destruction
}

inline void PushPullReceiver::start() {
    if (running_.load()) return;
    stop_requested_.store(false);
    worker_ = std::thread([this]() { receive_loop(); });
}

inline void PushPullReceiver::stop() noexcept {
    stop_requested_.store(true);
    if (socket_) {
        socket_->set(zmq::sockopt::linger, 0);  // Don't wait for pending messages on close
        socket_->close();
    }
    if (worker_.joinable()) worker_.join();
    running_.store(false);
}

inline void PushPullReceiver::receive_loop() {
    running_.store(true);

    try {
        // Create PULL socket on this thread — ZMQ sockets are not thread-safe
        socket_ = std::make_unique<zmq::socket_t>(context_, zmq::socket_type::pull);
        // zmq::socket_type::pull = the PULL end of PUSH/PULL
        // PULL binds; PUSH connects — receiver is the stable endpoint

        socket_->bind(endpoint_);   // Bind: this process is the stable listener
        // The Python PUSH socket does connect() to this address

        while (!stop_requested_.load()) {
            zmq::message_t msg;
            auto result = socket_->recv(msg, zmq::recv_flags::none);
            // Blocking recv — returns when message arrives or socket closes

            if (!result.has_value()) break;   // Socket was closed

            std::string raw(static_cast<char*>(msg.data()), msg.size());
            IncomingMessage parsed = parse(raw);

            if (parsed.type != IncomingMessageType::UNKNOWN && on_message_) {
                on_message_(std::move(parsed));
                // std::move: IncomingMessage holds optional<Order> which holds a string
                // (the symbol) — moving avoids copying the string
            }
        }
    } catch (const zmq::error_t& e) {
        if (e.num() != ETERM) {
            // Log unexpected ZMQ errors — ETERM is normal during shutdown
        }
    }

    running_.store(false);
}

inline IncomingMessage PushPullReceiver::parse(const std::string& raw) const {
    IncomingMessage result;  // type = UNKNOWN by default

    try {
        auto j = nlohmann::json::parse(raw);

        // Every message must have a "type" field
        std::string type_str = j.at("type").get<std::string>();

        if (type_str == "ORDER") {
            auto opt = parse_order(j);
            if (opt) {
                result.type  = IncomingMessageType::ORDER;
                result.order = std::move(opt);
            }
        } else if (type_str == "CANCEL") {
            auto opt = parse_cancel(j);
            if (opt) {
                result.type   = IncomingMessageType::CANCEL;
                result.cancel = std::move(opt);
            }
        } else if (type_str == "MARKET_DEPTH") {
            auto opt = parse_depth(j);
            if (opt) {
                result.type  = IncomingMessageType::MARKET_DEPTH;
                result.depth = std::move(opt);
            }
        }
        // Unknown type_str → result.type stays UNKNOWN → dropped by caller

    } catch (const nlohmann::json::exception&) {
        // Malformed JSON or missing required field — result stays UNKNOWN
    }

    return result;
}

inline std::optional<IncomingOrder>
PushPullReceiver::parse_order(const nlohmann::json& j) const {
    try {
        Order o;
        o.symbol     = j.at("symbol").get<std::string>();
        o.quantity   = j.at("quantity").get<uint32_t>();
        o.filled_qty = 0;
        o.status     = OrderStatus::NEW;

        // Parse side string → enum
        std::string side_str = j.at("side").get<std::string>();
        if      (side_str == "BUY")  o.side = Side::BUY;
        else if (side_str == "SELL") o.side = Side::SELL;
        else return std::nullopt;   // Unknown side — reject

        // Parse order type
        std::string type_str = j.at("order_type").get<std::string>();
        if (type_str == "LIMIT") {
            o.type      = OrderType::LIMIT;
            o.price_bps = j.at("price_bps").get<int64_t>();
            if (o.price_bps <= 0) return std::nullopt;
        } else if (type_str == "MARKET") {
            o.type      = OrderType::MARKET;
            o.price_bps = 0;
        } else {
            return std::nullopt;
        }

        return IncomingOrder{ std::move(o) };

    } catch (const nlohmann::json::exception&) {
        return std::nullopt;
    }
}

inline std::optional<IncomingCancel>
PushPullReceiver::parse_cancel(const nlohmann::json& j) const {
    try {
        return IncomingCancel{
            j.at("symbol").get<std::string>(),
            j.at("order_id").get<uint64_t>()
        };
    } catch (const nlohmann::json::exception&) {
        return std::nullopt;
    }
}

inline std::optional<IncomingMarketDepth>
PushPullReceiver::parse_depth(const nlohmann::json& j) const {
    try {
        std::string side_str = j.at("side").get<std::string>();
        Side side;
        if      (side_str == "BID") side = Side::BUY;
        else if (side_str == "ASK") side = Side::SELL;
        else return std::nullopt;

        return IncomingMarketDepth{
            j.at("symbol").get<std::string>(),
            side,
            j.at("price_bps").get<int64_t>(),
            j.at("qty").get<uint32_t>()
        };
    } catch (const nlohmann::json::exception&) {
        return std::nullopt;
    }
}
