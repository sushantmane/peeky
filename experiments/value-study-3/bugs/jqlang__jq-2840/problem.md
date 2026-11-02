Array indexing of negative indices wraps twice
**Describe the bug**
Negative indices should wrap only once, not twice.

**To Reproduce**
`jq -n '[0,1,2] | .[-5]'` produces `1`.

**Expected behavior**
Since the array in the reproduction example has length 3, `.[-5]` can be `.[3-5]`, but still out of index so should produce `null`.

**Environment (please complete the following information):**

- OS and Version: macOS (whatever)
- jq version: jq-1.7rc1-25-gf94a9d4

**Additional context**
Likely the regression of a6fe347322bfd57cab2d2612d8825b4ede765ac8.