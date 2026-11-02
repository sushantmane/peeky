tokio::select! with only an "else branch"
Hi, I wrote a procedural macro and in it I use a `tokio::select!` where the amount of branches are dependent on the proc macro's input.
I always have the falback `else` branch.

In my use case, my proc macro uses the user function signature as input and the amount of arguments will determine the amount of branches in the `tokio::select!` call. 

And it is totally valid to have a no argment function that will translate in a `select!` with 0 branch and only the `else` fallback. as such:

 ```rust
...
let foo = tokio::select! {
    else => 1,
};
...
```
But it doesn't compile.

**Describe the solution you'd like**

A clear and concise description of what you want to happen.
I would like this edge case to be valid. While I understand that this is not very useful by itself, we are talking here in the context of proc macro code.
Also, the `else` branch exists as a fallback for when all branches are disabled. And no branches at all is kinda equivalent to all branches disabled.

**Describe alternatives you've considered**
- **Alternative 1**: I write my proc macro a bit more complicate and take into account the special case when I have 0 branches and skip the `tokio::select!` altogether.

- **Alternative 2**: I added a permanent dummy branch always disabled (with a never type `!` return type)  in the expension of my proc macro. As such:
 ```rust
...
let foo = tokio::select! {
    _ = async {} , if false => unreachable!(),
    else => 1,
};
...
```
- **Alternative 3**: a PR to tokio to enhance the `select!` macro, the simplest is basically to implement "Alternative 2" directly in the `macro_rules!`.

- **Alternative 4**: a PR to tokio to enhance the `select!` macro but done more elegantly than "Alternative 3" but will required more complex changes in the macro and suporting files such `tokio\tokio-macros\src\select.rs` (because [here ](https://github.com/tokio-rs/tokio/blob/master/tokio-macros/src/select.rs#L34C20-L34C28) it doesn't work I think when `variants` is an empty Vec).

**Additional notes**
I already implemented **Alternative 3** and add 2 tests to cover it in `tokio\tokio\tests\macros_select.rs`.
I am ready to submit the PR if it is desired by the Tokio team.

Thank you.