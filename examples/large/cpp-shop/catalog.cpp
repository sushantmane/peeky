#include "catalog.hpp"
#include <algorithm>

namespace shop {

void Catalog::add(Item item) {
    for (const auto &tag : item.tags) by_tag_[tag].push_back(item.sku);
    std::string sku = item.sku;
    items_.emplace(std::move(sku), std::move(item));
}

const Item *Catalog::find(const std::string &sku) const {
    auto found = items_.find(sku);
    return found == items_.end() ? nullptr : &found->second;
}

std::vector<const Item *> Catalog::tagged(const std::string &tag) const {
    std::vector<const Item *> result;
    auto found = by_tag_.find(tag);
    if (found == by_tag_.end()) return result;
    for (const auto &sku : found->second) {
        if (const Item *item = find(sku)) result.push_back(item);
    }
    std::sort(result.begin(), result.end(),
              [](const Item *a, const Item *b) { return a->sku < b->sku; });
    return result;
}

Catalog demo_catalog() {
    struct Row {
        const char *sku;
        const char *name;
        Cents price;
        int grams;
        const char *tags;
    };
    static const Row rows[] = {
        {"BLT-10", "Bolt M10", 45, 12, "hardware bulk"},
        {"NUT-10", "Nut M10", 20, 6, "hardware bulk"},
        {"WSH-10", "Washer M10", 8, 2, "hardware bulk"},
        {"DRL-01", "Cordless drill", 8999, 1650, "tools power"},
        {"SAW-02", "Hand saw", 2450, 720, "tools"},
        {"GLV-03", "Work gloves", 1299, 140, "safety apparel"},
        {"HLM-04", "Hard hat", 2199, 410, "safety"},
        {"TAP-05", "Measuring tape", 1599, 260, "tools"},
        {"LVL-06", "Spirit level", 1899, 530, "tools"},
        {"SCR-07", "Screw 4x40", 5, 3, "hardware bulk"},
        {"ANC-08", "Wall anchor", 12, 2, "hardware bulk"},
        {"GOG-09", "Safety goggles", 999, 90, "safety apparel"},
    };
    Catalog catalog;
    for (const Row &row : rows) {
        Item item;
        item.sku = row.sku;
        item.name = row.name;
        item.unit_price = row.price;
        item.weight_grams = row.grams;
        std::string tags = row.tags;
        for (std::size_t at = 0; at < tags.size();) {
            std::size_t space = tags.find(' ', at);
            if (space == std::string::npos) space = tags.size();
            item.tags.insert(tags.substr(at, space - at));
            at = space + 1;
        }
        catalog.add(std::move(item));
    }
    return catalog;
}

}  // namespace shop
