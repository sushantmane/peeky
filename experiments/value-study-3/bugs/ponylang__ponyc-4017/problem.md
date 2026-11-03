Tuples can be smuggled into type constraints via type unions
```pony
type Blocksize is (None | (U8, U32))
class Block[T: Blocksize]
```

will crash the compiler. 

#4005 fixed part of the issue, but what is needed now is to take the fix code from there, move it to its own function so that we can also iterate over a typeunion as a constraint looking for any tuples in it.

```c
      ast_t* child = ast_child(constraint);
      token_id cap = cap_from_constraint(child);
      child = ast_sibling(child);

      while(child != NULL)
      {
        cap = NEW_IS_IT_A_TUPLE_SET_ERROR(child)
        child = ast_sibling(child);
      }
```

should be what is needed to iterate over and find any tuples.

`NEW_IS_IT_A_TUPLE_SET_ERROR` should probbably be something like `scan_constraint_for_tuples`.