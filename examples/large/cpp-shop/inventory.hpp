#pragma once
#include "catalog.hpp"
#include <deque>
#include <unordered_map>
#include <utility>

namespace shop {

struct Stock {
    int on_hand = 0;
    int reserved = 0;
    int available() const { return on_hand - reserved; }
};

class Inventory {
public:
    explicit Inventory(const Catalog &catalog);
    void receive(const std::string &sku, int units);
    bool reserve(const Order &order, std::vector<std::string> &shortages);
    void release(const Order &order);
    std::map<std::string, int> low_stock(int threshold) const;
    const Stock &stock(const std::string &sku) const;

private:
    const Catalog &catalog_;
    std::unordered_map<std::string, Stock> stock_;
    std::deque<std::pair<std::string, int>> journal_;
};

}  // namespace shop
