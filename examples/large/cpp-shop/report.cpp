#include "report.hpp"
#include <algorithm>
#include <cstdlib>
#include <iomanip>
#include <sstream>

namespace shop {

std::string money(Cents cents) {
    std::ostringstream out;
    out << (cents < 0 ? "-" : "") << '$' << std::llabs(cents) / 100 << '.' << std::setw(2)
        << std::setfill('0') << std::llabs(cents) % 100;
    return out.str();
}

Summary summarize(const std::vector<Quote> &quotes, const std::vector<Shipment> &plan) {
    Summary summary;
    std::vector<Cents> totals;
    totals.reserve(quotes.size());
    for (const auto &quote : quotes) {
        summary.revenue += quote.total();
        summary.discounts += quote.discount;
        totals.push_back(quote.total());
    }
    if (!totals.empty()) {
        summary.mean_order = static_cast<double>(summary.revenue) / totals.size();
        auto middle = totals.begin() + totals.size() / 2;
        std::nth_element(totals.begin(), middle, totals.end());
        summary.median_order = *middle;
    }
    for (const auto &shipment : plan) ++summary.shipments_per_day[shipment.day];
    return summary;
}

std::string render(const Summary &summary, const std::vector<Quote> &quotes) {
    std::ostringstream out;
    for (const auto &quote : quotes) {
        out << "order " << quote.order_id << ": " << money(quote.subtotal) << " - "
            << money(quote.discount) << " + " << money(quote.shipping) << " = " << money(quote.total());
        for (const auto &note : quote.notes) out << " [" << note << "]";
        out << '\n';
    }
    out << "revenue " << money(summary.revenue) << ", discounts " << money(summary.discounts)
        << ", mean " << std::fixed << std::setprecision(2) << summary.mean_order / 100
        << ", median " << money(summary.median_order) << '\n';
    for (const auto &[day, count] : summary.shipments_per_day) out << "day " << day << ": " << count << '\n';
    return out.str();
}

}  // namespace shop
