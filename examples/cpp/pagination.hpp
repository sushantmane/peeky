#pragma once

constexpr int PAGE_ITEM_MULTIPLIER = 10;
constexpr int MAX_PAGE_ITEMS = 128;

struct Pagination {
    int page;
    int page_size;
    int total;
};
