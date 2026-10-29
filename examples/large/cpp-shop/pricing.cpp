#include "pricing.hpp"
#include <numeric>
#include <type_traits>
#include <algorithm>

namespace shop {

Pricing::Pricing(const Catalog &catalog) : catalog_(catalog) {}

Cents Pricing::shipping_for(const Order &order, Cents subtotal) const {
    Cents shipping = subtotal >= 10000 ? 0 : 799;
    int grams = 0;
    for (const auto &line : order.lines) {
        if (const Item *item = catalog_.find(line.sku)) grams += item->weight_grams * line.quantity;
    }
    return shipping + (grams > 5000 ? (grams - 5000) / 1000 * 150 : 0);
}

void Pricing::add_coupon(const std::string &code, Rule rule) { coupons_.emplace(code, std::move(rule)); }

void Pricing::add_tier(const std::string &tier, int percent) { tiers_[tier] = percent; }

Cents Pricing::discount_for(const Rule &rule, const Order &order, Cents subtotal) const {
    return std::visit(
        [&](const auto &value) -> Cents {
            using T = std::decay_t<decltype(value)>;
            if constexpr (std::is_same_v<T, PercentOff>) {
                return subtotal * value.percent / 100;
            } else if constexpr (std::is_same_v<T, FixedOff>) {
                return subtotal >= value.minimum ? value.cents : 0;
            } else {
                int units = 0;
                Cents tagged = 0;
                for (const auto &line : order.lines) {
                    const Item *item = catalog_.find(line.sku);
                    if (item && item->tags.count(value.tag)) {
                        units += line.quantity;
                        tagged += item->unit_price * line.quantity;
                    }
                }
                return units >= value.units ? tagged * value.percent / 100 : 0;
            }
        },
        rule);
}

Quote Pricing::quote(const Order &order, const Customer &customer) const {
    Quote quote;
    quote.order_id = order.id;
    std::map<std::string, Cents> by_sku;
    for (const auto &line : order.lines) {
        const Item *item = catalog_.find(line.sku);
        if (!item) {
            quote.notes.push_back("unknown sku " + line.sku);
            continue;
        }
        by_sku[line.sku] += item->unit_price * line.quantity;
    }
    quote.subtotal = std::accumulate(by_sku.begin(), by_sku.end(), Cents{0},
                                     [](Cents sum, const auto &entry) { return sum + entry.second; });
    Cents tier_discount = 0;
    if (auto tier = tiers_.find(customer.tier); tier != tiers_.end()) {
        tier_discount = quote.subtotal * tier->second / 100;
    }
    Cents coupon_discount = 0;
    if (order.coupon) {
        auto found = coupons_.find(*order.coupon);
        if (found != coupons_.end()) {
            coupon_discount = discount_for(found->second, order, quote.subtotal);
        } else {
            quote.notes.push_back("unknown coupon " + *order.coupon);
        }
    }
    quote.discount = std::max(tier_discount, coupon_discount); /* OBSERVE */
    quote.shipping = shipping_for(order, quote.subtotal);
    return quote;
}

}  // namespace shop
