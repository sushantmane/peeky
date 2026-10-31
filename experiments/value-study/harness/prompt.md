You are fixing a bug in {repo}, a {language} project. The repository is at /testbed, at the commit where the bug was reported.

Your shell commands run in a Linux container with no network. Each command runs in a fresh shell that starts in /testbed, so use `cd dir && ...` or absolute paths; environment variables don't persist from one command to the next. The project's dependencies are installed. {test_hint}

Fix the bug in the project's source, not by changing existing tests. You may add tests. Finish with a short paragraph: the root cause and your fix.

The bug report follows.

<bug_report>
{problem}
</bug_report>
