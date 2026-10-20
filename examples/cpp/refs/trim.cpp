#include <cstdio>
#include <string>
static std::string trim_left(const std::string &s) {
    size_t i = 0;
    while (i < s.size() && s[i] == ' ') i++;
    return s.substr(i);
}
int main(int argc, char **argv) {
    for (int k = 1; k < argc; k++) {
        std::string name = trim_left(argv[k]);
        std::printf("[%s]\n", name.c_str());
    }
    return 0;
}
