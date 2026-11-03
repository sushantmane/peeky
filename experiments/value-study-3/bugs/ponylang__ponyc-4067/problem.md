Compiler crash in HeapToStack optimization
My offending code is in the following playground link:  https://playground.ponylang.io/?gist=6e4fdaad3b062b9e8a1bffe457e5e422

It compiles when I call ponyc with the ```-d``` option.  The compiler crashes (SEGV) when the ```-d``` is absent.

Here is the output from the compiler:
```
red@ub0:~/projects/pp$ ponyc -r=ir
Building builtin -> /home/red/.local/share/ponyup/ponyc-release-0.49.1-x86_64-linux-ubuntu20.04/packages/builtin
Building . -> /home/red/projects/pp
Generating
 Reachability
 Selector painting
 Data prototypes
 Data types
 Function prototypes
 Functions
 Descriptors
Optimising
PLEASE submit a bug report to https://bugs.llvm.org/ and include the crash backtrace.
Stack dump:
0.      Running pass 'Function Pass Manager' on module 'pp'.
1.      Running pass 'Move heap allocations to the stack' on function '@Main_Dispatch'
Segmentation fault (core dumped)
```

Here is the backtrace:
```
red@ub0:~/projects/pp$ lldb ponyc
(lldb) target create "ponyc"
Current executable set to 'ponyc' (x86_64).
(lldb) run
Process 23180 launched: '/home/red/.local/share/ponyup/bin/ponyc' (x86_64)
Building builtin -> /home/red/.local/share/ponyup/ponyc-release-0.49.1-x86_64-linux-ubuntu20.04/packages/builtin
Building . -> /home/red/projects/pp
Generating
 Reachability
 Selector painting
 Data prototypes
 Data types
 Function prototypes
 Functions
 Descriptors
Optimising
Process 23180 stopped
* thread #1, name = 'ponyc', stop reason = signal SIGSEGV: invalid address (fault address: 0x28)
    frame #0: 0x0000000001eafee4 ponyc`llvm::CallGraphNode::removeCallEdgeFor(llvm::CallBase&) + 52
ponyc`llvm::CallGraphNode::removeCallEdgeFor:
->  0x1eafee4 <+52>: addl   $-0x1, 0x28(%rax)
    0x1eafee8 <+56>: movq   0x18(%r14), %rbp
    0x1eafeec <+60>: leaq   -0x28(%rbp), %rsi
    0x1eafef0 <+64>: movq   %rbx, %rdi
(lldb) bt
* thread #1, name = 'ponyc', stop reason = signal SIGSEGV: invalid address (fault address: 0x28)
  * frame #0: 0x0000000001eafee4 ponyc`llvm::CallGraphNode::removeCallEdgeFor(llvm::CallBase&) + 52
    frame #1: 0x00000000007119dc ponyc`HeapToStack::runOnInstruction(llvm::IRBuilder<llvm::ConstantFolder, llvm::IRBuilderDefaultInserter>&, llvm::Instruction*, llvm::DominatorTree&, llvm::Function&) + 1196
    frame #2: 0x0000000000711475 ponyc`HeapToStack::runOnFunction(llvm::Function&) + 389
    frame #3: 0x000000000087706d ponyc`llvm::FPPassManager::runOnFunction(llvm::Function&) + 1053
    frame #4: 0x000000000087cd93 ponyc`llvm::FPPassManager::runOnModule(llvm::Module&) + 51
    frame #5: 0x0000000000877670 ponyc`llvm::legacy::PassManagerImpl::run(llvm::Module&) + 928
    frame #6: 0x000000000070e0a1 ponyc`genopt + 1121
    frame #7: 0x00000000006f8d07 ponyc`genexe + 535
    frame #8: 0x00000000006f07cf ponyc`codegen + 143
    frame #9: 0x00000000006e602a ponyc`main + 634
    frame #10: 0x00007ffff7c2d0b3 libc.so.6`__libc_start_main + 243
    frame #11: 0x00000000006e5cee ponyc`_start + 46
(lldb)  
```
If there is any other information or help I can help provide, please let me know.