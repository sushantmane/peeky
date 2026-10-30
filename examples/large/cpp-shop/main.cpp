#include "customers.hpp"
#include "inventory.hpp"
#include "orders.hpp"
#include "pricing.hpp"
#include "report.hpp"
#include "scheduler.hpp"
#include <cstdlib>
#include <iostream>

using namespace shop;

int main(int argc, char **argv) {
    int per_day = argc > 1 ? std::atoi(argv[1]) : 3;
    Catalog catalog = demo_catalog();
    Inventory inventory(catalog);
    for (const char *sku : {"BLT-10", "NUT-10", "WSH-10", "SCR-07", "ANC-08"}) inventory.receive(sku, 500);
    for (const char *sku : {"DRL-01", "SAW-02", "GLV-03", "HLM-04", "TAP-05", "LVL-06", "GOG-09"})
        inventory.receive(sku, 6);
    Customers customers = demo_customers();
    Pricing pricing(catalog);
    pricing.add_coupon("SPRING10", PercentOff{10});
    pricing.add_coupon("SAFETY5", TagBulk{"safety", 10, 5});
    pricing.add_coupon("BULK", FixedOff{2500, 20000});
    pricing.add_tier("gold", 8);
    pricing.add_tier("silver", 4);

    std::vector<std::string> errors;
    std::vector<Order> orders = parse_orders(demo_orders(), errors);
    std::vector<Quote> quotes;
    Scheduler scheduler(per_day);
    for (const auto &order : orders) {
        std::vector<std::string> shortages;
        if (!inventory.reserve(order, shortages)) {
            for (const auto &shortage : shortages) errors.push_back("order " + std::to_string(order.id) + ": " + shortage);
            continue;
        }
        auto customer = customers.find(order.customer_id);
        if (!customer) {
            inventory.release(order);
            errors.push_back("order " + std::to_string(order.id) + ": unknown customer");
            continue;
        }
        Quote quote = pricing.quote(order, *customer);
        customers.record(order);
        scheduler.submit(order, quote.total());
        quotes.push_back(std::move(quote));
    }
    auto plan = scheduler.plan();
    Summary summary = summarize(quotes, plan);
    std::cout << render(summary, quotes);
    for (const auto &[name, units] : customers.top_buyers(2)) std::cout << "top " << name << ' ' << units << '\n';
    for (const auto &[sku, left] : inventory.low_stock(3)) std::cout << "low " << sku << ' ' << left << '\n';
    for (const auto &error : errors) std::cout << "error " << error << '\n';
    return 0;
}
