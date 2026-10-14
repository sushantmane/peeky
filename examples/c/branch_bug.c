#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) printf("ARG:%zu:%s\n", strlen(argv[i]), argv[i]);
    int value = 0;
    int *pointer = &value;
    int items[3] = {2, 4, 6};
    int pid = (int)getpid();
    int token_present = getenv("DEBUG_TOKEN") != NULL;
    const char *result = value > 0 ? "success" : "fallback"; /* FIRST */
    printf("RESULT=%s value=%d array=%d pointer=%d\n", result, value, items[1], *pointer); /* SECOND */
    return 0;
}
