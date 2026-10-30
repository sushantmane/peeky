#include "orders.hpp"
#include <sstream>

namespace shop {

namespace {

std::vector<std::string> split(const std::string &text, char separator) {
    std::vector<std::string> parts;
    std::string part;
    std::istringstream stream(text);
    while (std::getline(stream, part, separator)) {
        auto first = part.find_first_not_of(' ');
        auto last = part.find_last_not_of(' ');
        parts.push_back(first == std::string::npos ? "" : part.substr(first, last - first + 1));
    }
    return parts;
}

}  // namespace

std::optional<Order> parse_order(const std::string &line, std::string &error) {
    auto fields = split(line, '|');
    if (fields.size() < 3) {
        error = "expected id|customer|lines[|coupon][|priority]: " + line;
        return std::nullopt;
    }
    Order order;
    try {
        order.id = std::stoi(fields[0]);
        order.customer_id = std::stoi(fields[1]);
        if (fields.size() > 4) order.priority = std::stoi(fields[4]);
    } catch (const std::exception &) {
        error = "bad number in: " + line;
        return std::nullopt;
    }
    for (const auto &entry : split(fields[2], ',')) {
        auto pair = split(entry, 'x');
        if (pair.size() != 2) {
            error = "bad line " + entry;
            return std::nullopt;
        }
        order.lines.push_back({pair[0], std::stoi(pair[1])});
    }
    if (fields.size() > 3 && !fields[3].empty()) order.coupon = fields[3];
    return order;
}

std::vector<Order> parse_orders(const std::string &text, std::vector<std::string> &errors) {
    std::vector<Order> orders;
    std::istringstream stream(text);
    std::string line;
    while (std::getline(stream, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::string error;
        if (auto order = parse_order(line, error)) {
            orders.push_back(std::move(*order));
        } else {
            errors.push_back(error);
        }
    }
    return orders;
}

std::string demo_orders() {
    return "# id|customer|sku x qty,...|coupon|priority\n"
           "1001|7|BLT-10x200,NUT-10x200,WSH-10x400||1\n"
           "1002|3|DRL-01x1,GLV-03x2|SPRING10|2\n"
           "1003|7|SAW-02x1,TAP-05x1,LVL-06x1||0\n"
           "1004|9|HLM-04x5,GOG-09x5,GLV-03x5|SAFETY5|3\n"
           "1005|3|SCR-07x1000,ANC-08x250||1\n"
           "1006|12|DRL-01x2,SAW-02x2|BULK|0\n"
           "1007|9|BLT-10x50,NUT-10x50||2\n"
           "1008|7|TAP-05x3,LVL-06x2,GLV-03x1|SPRING10|1\n";
}

}  // namespace shop
