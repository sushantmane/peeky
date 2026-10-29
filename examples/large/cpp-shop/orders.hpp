#pragma once
#include "common.hpp"

namespace shop {

std::optional<Order> parse_order(const std::string &line, std::string &error);
std::vector<Order> parse_orders(const std::string &text, std::vector<std::string> &errors);
std::string demo_orders();

}  // namespace shop
