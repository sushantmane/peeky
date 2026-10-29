#pragma once
#include "scheduler.hpp"

namespace shop {

struct Summary {
    Cents revenue = 0;
    Cents discounts = 0;
    double mean_order = 0;
    Cents median_order = 0;
    std::map<int, int> shipments_per_day;
};

Summary summarize(const std::vector<Quote> &quotes, const std::vector<Shipment> &plan);
std::string render(const Summary &summary, const std::vector<Quote> &quotes);
std::string money(Cents cents);

}  // namespace shop
