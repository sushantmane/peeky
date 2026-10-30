#pragma once
#include "common.hpp"
#include <queue>
#include <tuple>

namespace shop {

struct Shipment {
    int order_id = 0;
    int priority = 0;
    int day = 0;
    Cents value = 0;
};

class Scheduler {
public:
    explicit Scheduler(int per_day) : per_day_(per_day) {}
    void submit(const Order &order, Cents value);
    std::vector<Shipment> plan();

private:
    struct Pending {
        int priority;
        Cents value;
        int order_id;
        bool operator<(const Pending &other) const {
            return std::tie(priority, value, other.order_id) < std::tie(other.priority, other.value, order_id);
        }
    };
    int per_day_;
    std::priority_queue<Pending> queue_;
};

}  // namespace shop
