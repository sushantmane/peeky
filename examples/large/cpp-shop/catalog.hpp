#pragma once
#include "common.hpp"
#include <unordered_map>

namespace shop {

class Catalog {
public:
    void add(Item item);
    const Item *find(const std::string &sku) const;
    std::vector<const Item *> tagged(const std::string &tag) const;
    std::size_t size() const { return items_.size(); }

private:
    std::unordered_map<std::string, Item> items_;
    std::map<std::string, std::vector<std::string>> by_tag_;
};

Catalog demo_catalog();

}  // namespace shop
