expr pass: Assertion error on iftype inside lambda
This code:

```pony
actor Main
  new create(env: Env) => None

  fun test[T: (U8 | I8)](arg: T) =>
    let do_it = {(v: T): T =>
      iftype T <: U8 then
        v
      else
        v
      end
    }
    do_it(arg)
```

Produces the following error:

```
0.41.1-4553e6cf [release]
Compiled with: LLVM 9.0.1 -- Clang-9.0.0-x86_64
Defaults: pic=true
/tmp/cirrus-ci-build/src/libponyc/pass/scope.c:291: scope_iftype: Assertion `ast_id(typeparam_store) == TK_NONE` failed.
```

<details><summary>Backtrace</summary>
<pre>
* thread #1, queue = 'com.apple.main-thread', stop reason = signal SIGABRT
  * frame #0: 0x00007fff5efb82c2 libsystem_kernel.dylib`__pthread_kill + 10
    frame #1: 0x00007fff5f073bf1 libsystem_pthread.dylib`pthread_kill + 284
    frame #2: 0x00007fff5ef226a6 libsystem_c.dylib`abort + 127
    frame #3: 0x0000000100115c4d ponyc`ponyint_assert_fail(expr="ast_id(typeparam_store) == TK_NONE", file="/Users/ryan/dev/ponyc/src/libponyc/pass/scope.c", line=291, func="scope_iftype") at ponyassert.c:65:3
    frame #4: 0x00000001000be1e6 ponyc`scope_iftype(opt=0x00007ffeefbff4b8, ast=0x000000010acdc980) at scope.c:291:3
    frame #5: 0x00000001000bd8c5 ponyc`pass_scope(astp=0x00007ffeefbfdfe0, options=0x00007ffeefbff4b8) at scope.c:360:14
    frame #6: 0x00000001000b6ff3 ponyc`ast_visit(ast=0x00007ffeefbfdfe0, pre=(ponyc`pass_scope at scope.c:324), post=0x0000000000000000, options=0x00007ffeefbff4b8, pass=PASS_SCOPE) at pass.c:399:12
    frame #7: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbfe070, pre=(ponyc`pass_scope at scope.c:324), post=0x0000000000000000, options=0x00007ffeefbff4b8, pass=PASS_SCOPE) at pass.c:428:14
    frame #8: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbfe100, pre=(ponyc`pass_scope at scope.c:324), post=0x0000000000000000, options=0x00007ffeefbff4b8, pass=PASS_SCOPE) at pass.c:428:14
    frame #9: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbfe190, pre=(ponyc`pass_scope at scope.c:324), post=0x0000000000000000, options=0x00007ffeefbff4b8, pass=PASS_SCOPE) at pass.c:428:14
    frame #10: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbfe220, pre=(ponyc`pass_scope at scope.c:324), post=0x0000000000000000, options=0x00007ffeefbff4b8, pass=PASS_SCOPE) at pass.c:428:14
    frame #11: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbfe730, pre=(ponyc`pass_scope at scope.c:324), post=0x0000000000000000, options=0x00007ffeefbff4b8, pass=PASS_SCOPE) at pass.c:428:14
    frame #12: 0x00000001000b7c9a ponyc`visit_pass(astp=0x00007ffeefbfe730, options=0x00007ffeefbff4b8, last_pass=PASS_EXPR, out_r=0x00007ffeefbfe323, pass=PASS_SCOPE, pre_fn=(ponyc`pass_scope at scope.c:324), post_fn=0x0000000000000000) at pass.c:176:6
    frame #13: 0x00000001000b73c5 ponyc`ast_passes(astp=0x00007ffeefbfe730, options=0x00007ffeefbff4b8, last=PASS_EXPR) at pass.c:225:7
    frame #14: 0x00000001000b7995 ponyc`ast_passes_type(astp=0x00007ffeefbfe730, options=0x00007ffeefbff4b8, last_pass=PASS_EXPR) at pass.c:339:13
    frame #15: 0x000000010009cf00 ponyc`expr_object(opt=0x00007ffeefbff4b8, astp=0x00007ffeefbfee80) at lambda.c:826:7
    frame #16: 0x00000001000b303e ponyc`pass_expr(astp=0x00007ffeefbfee80, options=0x00007ffeefbff4b8) at expr.c:610:11
    frame #17: 0x00000001000b7183 ponyc`ast_visit(ast=0x00007ffeefbfee80, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:457:12
    frame #18: 0x00000001000b7c9a ponyc`visit_pass(astp=0x00007ffeefbfee80, options=0x00007ffeefbff4b8, last_pass=PASS_EXPR, out_r=0x00007ffeefbfe973, pass=PASS_EXPR, pre_fn=(ponyc`pass_pre_expr at expr.c:521), post_fn=(ponyc`pass_expr at expr.c:538)) at pass.c:176:6
    frame #19: 0x00000001000b7675 ponyc`ast_passes(astp=0x00007ffeefbfee80, options=0x00007ffeefbff4b8, last=PASS_EXPR) at pass.c:269:7
    frame #20: 0x00000001000b7a13 ponyc`ast_passes_subtree(astp=0x00007ffeefbfee80, options=0x00007ffeefbff4b8, last_pass=PASS_EXPR) at pass.c:351:10
    frame #21: 0x000000010009a79c ponyc`expr_lambda(opt=0x00007ffeefbff4b8, astp=0x00007ffeefbfee80) at lambda.c:456:10
    frame #22: 0x00000001000b3064 ponyc`pass_expr(astp=0x00007ffeefbfee80, options=0x00007ffeefbff4b8) at expr.c:616:11
    frame #23: 0x00000001000b7183 ponyc`ast_visit(ast=0x00007ffeefbfee80, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:457:12
    frame #24: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbfef10, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:428:14
    frame #25: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbfefa0, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:428:14
    frame #26: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbff030, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:428:14
    frame #27: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbff0c0, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:428:14
    frame #28: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbff150, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:428:14
    frame #29: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbff1e0, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:428:14
    frame #30: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbff270, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:428:14
    frame #31: 0x00000001000b70bc ponyc`ast_visit(ast=0x00007ffeefbff3a8, pre=(ponyc`pass_pre_expr at expr.c:521), post=(ponyc`pass_expr at expr.c:538), options=0x00007ffeefbff4b8, pass=PASS_EXPR) at pass.c:428:14
    frame #32: 0x00000001000b7c9a ponyc`visit_pass(astp=0x00007ffeefbff3a8, options=0x00007ffeefbff4b8, last_pass=PASS_ALL, out_r=0x00007ffeefbff373, pass=PASS_EXPR, pre_fn=(ponyc`pass_pre_expr at expr.c:521), post_fn=(ponyc`pass_expr at expr.c:538)) at pass.c:176:6
    frame #33: 0x00000001000b7675 ponyc`ast_passes(astp=0x00007ffeefbff3a8, options=0x00007ffeefbff4b8, last=PASS_ALL) at pass.c:269:7
    frame #34: 0x00000001000b7282 ponyc`ast_passes_program(ast=0x000000010bfbfd00, options=0x00007ffeefbff4b8) at pass.c:318:10
    frame #35: 0x00000001000dcddf ponyc`program_load(path="iftype_test/", opt=0x00007ffeefbff4b8) at package.c:934:7
    frame #36: 0x000000010000223f ponyc`compile_package(path="iftype_test/", opt=0x00007ffeefbff4b8, print_program_ast=false, print_package_ast=false) at main.c:56:20
    frame #37: 0x000000010000215f ponyc`main(argc=2, argv=0x00007ffeefbff5c0) at main.c:112:15
    frame #38: 0x00007fff5ee7d3d5 libdyld.dylib`start + 1
</pre>
</details>

Code snippet here:

https://github.com/ponylang/ponyc/blob/285ff8ad9830a59146e60efe84cea68122f895c4/src/libponyc/pass/scope.c#L288-L293

In the above code, `ast_id(typeparam_store)` evaluates to `TK_TYPEPARAMS`, so it seems that the compiler might have run the scope pass twice for this function (one for the original function, then again when expanding the lambda).