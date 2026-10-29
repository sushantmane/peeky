#include "scheduler.hpp"

namespace shop {

void Scheduler::submit(const Order &order, Cents value) {
    queue_.push({order.priority, value, order.id});
}

std::vector<Shipment> Scheduler::plan() {
    std::vector<Shipment> plan;
    int day = 0;
    int today = 0;
    while (!queue_.empty()) {
        Pending next = queue_.top();
        queue_.pop();
        if (today == per_day_) {
            ++day;
            today = 0;
        }
        plan.push_back({next.order_id, next.priority, day, next.value});
        ++today;
    }
    return plan;
}

}  // namespace shop
