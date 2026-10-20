#include <cstdio>
#include <map>
#include <set>
#include <string>
int main() {
    std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}};
    std::set<int> s{3, 1, 2};
    std::map<std::string, int> ages{{"bo", 31}, {"al", 42}};
    std::printf("%d %zu %d\n", m[1], s.size(), ages["al"]);
    return 0;
}
