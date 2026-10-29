#pragma once
#include "catalog.hpp"
#include <unordered_map>
#include <variant>

namespace shop {

struct PercentOff { int percent; };
struct FixedOff { Cents cents; Cents minimum; };
struct TagBulk { std::string tag; int units; int percent; };
using Rule = std::variant<PercentOff, FixedOff, TagBulk>;

class Pricing {
public:
    explicit Pricing(const Catalog &catalog);
    void add_coupon(const std::string &code, Rule rule);
    void add_tier(const std::string &tier, int percent);
    Quote quote(const Order &order, const Customer &customer) const;

private:
    Cents discount_for(const Rule &rule, const Order &order, Cents subtotal) const;
    const Catalog &catalog_;
    std::unordered_map<std::string, Rule> coupons_;
    std::map<std::string, int> tiers_;
    Cents shipping_for(const Order &order, Cents subtotal) const;
};

}  // namespace shop
