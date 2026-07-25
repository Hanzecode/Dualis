#pragma once

// ─────────────────────────────────────────────────────────────────────────────
//  ZMQ SIGNAL CONSUMER
//  Receives alpha signals from Python (momentum/Ridge regression engine)
//  via ZeroMQ SUB socket and translates them into Order objects.
//
//  Architecture:
//    Python (alpha engine) → ZMQ PUB → [network] → ZMQ SUB → this class → OrderBook
//
//  Message format (JSON-like, newline-delimited for easy Python serialisation):
//    {"symbol":"AAPL","signal":0.73,"confidence":0.85,"target_qty":500}
//
//  This file uses the ZeroMQ C++ header-only bindings (zmq.hpp / zmq_addon.hpp)
//  Install: vcpkg install cppzmq  OR  apt install libzmq3-dev
// ─────────────────────────────────────────────────────────────────────────────

#include <zmq.hpp>             // ZeroMQ C++ bindings — context, socket, message
#include <zmq_addon.hpp>       // multipart_t — for receiving multi-frame messages
#include <nlohmann/json.hpp>   // JSON parsing — install: vcpkg install nlohmann-json
#include <thread>              // std::thread — runs the receive loop on a background thread
#include <atomic>              // std::atomic<bool> — stop flag, safe across threads
#include <functional>          // std::function — callback for when a signal arrives
#include <string>
#include <stdexcept>
#include <chrono>

#include "order_book.hpp"      // For Order type that we construct from signals
#include "risk_manager.hpp"    // For RiskManager we run pre-trade checks through

// ─────────────────────────────────────────────────────────────────────────────
//  ALPHA SIGNAL STRUCT
//  The parsed representation of one message from the Python alpha engine.
//  Signal is a float in [-1.0, +1.0]:
//    +1.0 = very strong buy
//    -1.0 = very strong sell
//     0.0 = neutral / flat
// ─────────────────────────────────────────────────────────────────────────────

struct AlphaSignal {
    std::string symbol;
    double      signal;        // Normalised alpha: [-1.0, +1.0]
    double      confidence;    // Model confidence: [0.0, 1.0] from Ridge regression
    uint32_t    target_qty;    // Absolute target position size suggested by the model
    int64_t     limit_price_bps; // Suggested limit price (0 = use market)
};

// ─────────────────────────────────────────────────────────────────────────────
//  SIGNAL CONSUMER CLASS
//
//  Threading model:
//  - Main thread constructs and calls start()
//  - Background thread runs receive_loop() — blocks on zmq::socket_t::recv()
//  - On each received signal, the on_signal_ callback is called from the
//    background thread. If that callback touches shared state (e.g. the order
//    book), ensure the order book is thread-safe, or marshal back to main thread.
//
//  Lifetime: call stop() before destruction to join the background thread.
//  Destructor calls stop() as a safety net.
// ─────────────────────────────────────────────────────────────────────────────

class ZmqSignalConsumer {
public:
    // Callback signature: called for each successfully parsed signal
    using SignalCallback = std::function<void(const AlphaSignal&)>;

    // Constructor — does NOT start the thread; call start() explicitly
    // endpoint example: "tcp://localhost:5555"
    ZmqSignalConsumer(std::string endpoint,
                      std::string topic,        // ZMQ subscription topic filter, e.g. "signal"
                      SignalCallback on_signal);

    // Destructor — ensures background thread is stopped and joined
    // 'noexcept': destructors must not throw (if they do during stack unwinding, std::terminate)
    ~ZmqSignalConsumer() noexcept;

    // Start receiving — launches background thread
    void start();

    // Stop receiving — sets stop flag, interrupts recv, joins thread
    void stop() noexcept;

    [[nodiscard]] bool running() const { return running_.load(); }

private:
    std::string      endpoint_;     // ZMQ address to connect to
    std::string      topic_;        // Subscription topic

    // ZMQ context — manages I/O threads. One context per process is the recommendation.
    // zmq::context_t wraps the opaque zmq_ctx_t; RAII destructor closes it.
    zmq::context_t   context_;

    // Socket — created inside receive_loop() on the background thread.
    // ZMQ sockets are NOT thread-safe: must be created and used on the same thread.
    // We can't declare it here as a member if we create it on the worker thread —
    // use a unique_ptr so we can initialise it lazily on the worker thread.
    std::unique_ptr<zmq::socket_t> socket_;

    SignalCallback   on_signal_;

    std::thread      worker_;           // Background receive thread
    std::atomic<bool> running_;         // True while the receive loop should continue
    std::atomic<bool> stop_requested_;  // Set by stop() to break the loop

    // The receive loop — runs on worker_ thread
    void receive_loop();

    // Parse a raw ZMQ message string into an AlphaSignal
    // Returns std::nullopt if parsing fails (malformed JSON, missing fields)
    [[nodiscard]] std::optional<AlphaSignal> parse_message(const std::string& raw) const;

    // Convert an AlphaSignal into an Order
    // Signal strength determines side and quantity
    [[nodiscard]] Order signal_to_order(const AlphaSignal& signal) const;
};

// ─────────────────────────────────────────────────────────────────────────────
//  IMPLEMENTATION
// ─────────────────────────────────────────────────────────────────────────────

inline ZmqSignalConsumer::ZmqSignalConsumer(std::string endpoint,
                                             std::string topic,
                                             SignalCallback on_signal)
    : endpoint_(std::move(endpoint))
    , topic_(std::move(topic))
    , context_(1)              // 1 = number of I/O threads in the context
    , on_signal_(std::move(on_signal))
    , running_(false)
    , stop_requested_(false)
{}

inline ZmqSignalConsumer::~ZmqSignalConsumer() noexcept {
    stop();    // Ensure thread is joined — undefined behaviour to destroy a joinable thread
}

inline void ZmqSignalConsumer::start() {
    if (running_.load()) {
        throw std::runtime_error("ZmqSignalConsumer::start: already running");
    }
    stop_requested_.store(false);
    // Launch background thread — std::thread constructor takes a callable
    // We pass a lambda that captures 'this' by pointer; safe because 'this'
    // outlives the thread (stop() joins before destructor completes)
    worker_ = std::thread([this]() { receive_loop(); });
}

inline void ZmqSignalConsumer::stop() noexcept {
    stop_requested_.store(true);

    // Close the socket to unblock recv() — zmq recv blocks until data or interrupt
    // Setting linger to 0 means close() returns immediately (don't wait for pending msgs)
    if (socket_) {
        socket_->set(zmq::sockopt::linger, 0);  // sockopt::linger = ZMQ_LINGER option
        socket_->close();
    }

    // Join the thread — blocks until receive_loop() returns
    if (worker_.joinable()) {    // joinable() = thread has been started and not yet joined
        worker_.join();
    }
    running_.store(false);
}

inline void ZmqSignalConsumer::receive_loop() {
    running_.store(true);

    try {
        // Create socket ON THIS THREAD — ZMQ sockets are not thread-safe
        socket_ = std::make_unique<zmq::socket_t>(context_, zmq::socket_type::sub);
        // zmq::socket_type::sub = subscriber socket in the PUB/SUB pattern

        // Subscribe to the topic — empty string "" = subscribe to everything
        socket_->set(zmq::sockopt::subscribe, topic_);

        // Connect to the publisher
        socket_->connect(endpoint_);

        while (!stop_requested_.load()) {
            zmq::message_t msg;

            // Blocking receive — returns when a message arrives or socket is closed
            // recv() returns std::optional<size_t> in the C++ bindings
            auto result = socket_->recv(msg, zmq::recv_flags::none);

            if (!result.has_value()) {
                // recv returned without data — socket was interrupted or closed
                break;
            }

            // Convert ZMQ message to std::string for JSON parsing
            std::string raw(static_cast<char*>(msg.data()), msg.size());
            // msg.data() returns void* — cast to char* for string construction

            // Skip the topic prefix if present — ZMQ SUB delivers [topic][data] as one frame
            // or as two frames depending on publisher. Handle both.
            std::string payload = raw;
            if (raw.size() > topic_.size() && raw.substr(0, topic_.size()) == topic_) {
                payload = raw.substr(topic_.size() + 1);  // +1 to skip the space separator
            }

            // Parse and dispatch
            auto signal = parse_message(payload);
            if (signal.has_value() && on_signal_) {
                on_signal_(*signal);   // Dereference optional, call the callback
            }
            // Silently drop malformed messages — log in production instead
        }
    } catch (const zmq::error_t& e) {
        // zmq::error_t inherits from std::exception; errno is in e.num()
        if (e.num() != ETERM) {
            // ETERM = context was terminated — normal during shutdown; don't re-throw
            // Any other ZMQ error is unexpected — log it
            // In production: use spdlog::error("ZMQ error: {}", e.what());
        }
    }

    running_.store(false);
}

inline std::optional<AlphaSignal>
ZmqSignalConsumer::parse_message(const std::string& raw) const {
    try {
        // nlohmann::json::parse throws on invalid JSON
        auto j = nlohmann::json::parse(raw);

        // .at() throws std::out_of_range if key missing — we catch below
        // .get<T>() converts JSON value to C++ type — throws if wrong type
        AlphaSignal sig;
        sig.symbol      = j.at("symbol").get<std::string>();
        sig.signal      = j.at("signal").get<double>();
        sig.confidence  = j.at("confidence").get<double>();
        sig.target_qty  = j.at("target_qty").get<uint32_t>();
        // limit_price_bps is optional in the message — use value_or default
        sig.limit_price_bps = j.value("limit_price_bps", int64_t{0});
        // j.value(key, default) returns the value if key exists, default otherwise

        // Validate signal is in expected range
        if (sig.signal < -1.0 || sig.signal > 1.0) return std::nullopt;
        if (sig.confidence < 0.0 || sig.confidence > 1.0) return std::nullopt;

        return sig;

    } catch (const nlohmann::json::exception&) {
        // Catch-by-base: handles parse_error, out_of_range, type_error
        return std::nullopt;  // Caller will drop this message
    }
}

inline Order ZmqSignalConsumer::signal_to_order(const AlphaSignal& sig) const {
    Order order;
    order.symbol = sig.symbol;

    // Signal > 0 = buy, signal < 0 = sell
    // Threshold of 0.1 to avoid placing orders on noise
    if (sig.signal > 0.1) {
        order.side = Side::BUY;
    } else if (sig.signal < -0.1) {
        order.side = Side::SELL;
    }
    // Caller should check signal strength and not call this for |signal| <= 0.1

    // Scale order quantity by confidence — low confidence → smaller size
    // static_cast<uint32_t>: double → uint32, truncates (intentional for order sizing)
    order.quantity = static_cast<uint32_t>(sig.target_qty * sig.confidence);

    if (sig.limit_price_bps > 0) {
        order.type        = OrderType::LIMIT;
        order.price_bps   = sig.limit_price_bps;
    } else {
        order.type        = OrderType::MARKET;
        order.price_bps   = 0;   // Market orders don't have a price
    }

    // order_id, filled_qty, status, timestamp are assigned by OrderBook::submit()
    return order;
}
