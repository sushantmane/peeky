#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <array>
#include "pagination.hpp"

static int parse_count(const char *text, int minimum, int *result) {
    char *end;
    errno = 0;
    long value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' ||
        value < minimum || value > MAX_PAGE_ITEMS) return 0;
    *result = (int)value;
    return 1;
}

int main(int argc, char **argv) {
    int page = 1, page_size = 3, total = 8;
    if (argc > 4 ||
        (argc > 1 && !parse_count(argv[1], 1, &page)) ||
        (argc > 2 && !parse_count(argv[2], 1, &page_size)) ||
        (argc > 3 && !parse_count(argv[3], 0, &total))) return 2;
    Pagination pagination{page, page_size, total};
    Pagination &request = pagination;
    std::array<int, MAX_PAGE_ITEMS> items{};
    for (int i = 0; i < total; i++) items[i] = (i + 1) * PAGE_ITEM_MULTIPLIER;
    int start = request.page * request.page_size;
    int end = start + page_size; /* OBSERVE */
    if (end > total) end = total;
    printf("PAGE:[");
    for (int i = start; i < end; i++) printf("%s%d", i > start ? ", " : "", items[i]);
    printf("]\n");
    return 0;
}
