#pragma once
#include "common.hpp"
#include <memory>
#include <utility>

namespace shop {

class Customers {
public:
    void add(Customer customer);
    std::shared_ptr<const Customer> find(int id) const;
    void record(const Order &order);
    std::vector<std::pair<std::string, int>> top_buyers(std::size_t count) const;

private:
    std::map<int, std::shared_ptr<Customer>> by_id_;
};

Customers demo_customers();

}  // namespace shop
