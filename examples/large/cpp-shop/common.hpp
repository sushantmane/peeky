#pragma once
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace shop {

using Cents = std::int64_t;

struct Item {
    std::string sku;
    std::string name;
    Cents unit_price = 0;
    int weight_grams = 0;
    std::set<std::string> tags;
};

struct OrderLine {
    std::string sku;
    int quantity = 0;
};

struct Order {
    int id = 0;
    int customer_id = 0;
    std::vector<OrderLine> lines;
    std::optional<std::string> coupon;
    int priority = 0;
};

struct Customer {
    int id = 0;
    std::string name;
    std::string tier;
    std::map<std::string, int> history;
};

struct Quote {
    int order_id = 0;
    Cents subtotal = 0;
    Cents discount = 0;
    Cents shipping = 0;
    Cents total() const { return subtotal - discount + shipping; }
    std::vector<std::string> notes;
};

}  // namespace shop
