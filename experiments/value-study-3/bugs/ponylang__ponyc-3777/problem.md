Flags.remove mutating to an incorrect value.
```
    t = set0011.clone()              //0b0011
    t.remove(set0101)                //0b0101
    h.assert_true(t == _TestFlagsFlags(0b0010))
```

The test fails as it's mutating to 0b0110.

PR incoming.