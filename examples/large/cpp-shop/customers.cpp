#include "customers.hpp"
#include <algorithm>

namespace shop {

void Customers::add(Customer customer) {
    int id = customer.id;
    by_id_[id] = std::make_shared<Customer>(std::move(customer));
}

std::shared_ptr<const Customer> Customers::find(int id) const {
    auto found = by_id_.find(id);
    if (found == by_id_.end()) return nullptr;
    return found->second;
}

void Customers::record(const Order &order) {
    auto found = by_id_.find(order.customer_id);
    if (found == by_id_.end()) return;
    for (const auto &line : order.lines) found->second->history[line.sku] += line.quantity;
}

std::vector<std::pair<std::string, int>> Customers::top_buyers(std::size_t count) const {
    std::vector<std::pair<std::string, int>> totals;
    for (const auto &[id, customer] : by_id_) {
        int units = 0;
        for (const auto &[sku, quantity] : customer->history) units += quantity;
        totals.emplace_back(customer->name, units);
    }
    std::stable_sort(totals.begin(), totals.end(),
                     [](const auto &a, const auto &b) { return a.second > b.second; });
    if (totals.size() > count) totals.resize(count);
    return totals;
}

Customers demo_customers() {
    Customers customers;
    customers.add({3, "Ana Builders", "gold", {}});
    customers.add({7, "Bo's Hardware", "silver", {}});
    customers.add({9, "City Works", "public", {}});
    customers.add({12, "Dee Renovations", "", {}});
    return customers;
}

}  // namespace shop
