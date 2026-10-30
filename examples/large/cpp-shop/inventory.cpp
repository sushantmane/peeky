#include "inventory.hpp"

#include <stdexcept>
#include <algorithm>
#include <sstream>

namespace shop {

Inventory::Inventory(const Catalog &catalog) : catalog_(catalog) {}

void Inventory::receive(const std::string &sku, int units) {
    if (!catalog_.find(sku)) throw std::invalid_argument("unknown sku " + sku);
    stock_[sku].on_hand += units;
    journal_.emplace_back(sku, units);
    if (journal_.size() > 64) journal_.pop_front();
}

bool Inventory::reserve(const Order &order, std::vector<std::string> &shortages) {
    std::unordered_map<std::string, int> wanted;
    for (const auto &line : order.lines) wanted[line.sku] += line.quantity;
    for (const auto &[sku, units] : wanted) {
        auto found = stock_.find(sku);
        int available = found == stock_.end() ? 0 : found->second.available();
        if (available < units) {
            std::ostringstream note;
            note << sku << " short by " << (units - available);
            shortages.push_back(note.str());
        }
    }
    if (!shortages.empty()) return false;
    for (const auto &[sku, units] : wanted) {
        stock_[sku].reserved += units;
        journal_.emplace_back(sku, -units);
    }
    return true;
}

void Inventory::release(const Order &order) {
    for (const auto &line : order.lines) {
        auto &entry = stock_[line.sku];
        entry.reserved = std::max(0, entry.reserved - line.quantity);
    }
}

std::map<std::string, int> Inventory::low_stock(int threshold) const {
    std::map<std::string, int> low;
    for (const auto &[sku, entry] : stock_) {
        if (entry.available() < threshold) low.emplace(sku, entry.available());
    }
    return low;
}

const Stock &Inventory::stock(const std::string &sku) const {
    static const Stock none;
    auto found = stock_.find(sku);
    return found == stock_.end() ? none : found->second;
}

}  // namespace shop
