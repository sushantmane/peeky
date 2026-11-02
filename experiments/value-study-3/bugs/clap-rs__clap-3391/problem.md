Option not to reorder possible values for an argument
### Affected Version of clap

2.33.0

### Bug or Feature Request Summary

I have a list of possible *numeric* values for a given argument, but clap reorders them in alphabetical (non-numeric) order.

### Expected Behavior Summary

Being able to tell clap not to reorder my argument list.

### Actual Behavior Summary

A list of numbers (e.g. ["0", "1", "2", "12"]) is reordered in non-numeric order (e.g. ["0", "1", "12", "2"]).

### Sample Code or Link to Sample Code

```
let number_strings = ["0", "1", "2", "12"];

(boilerplate)
.arg(
    Arg::with_name("my_arg")
    .short("m")
    .long("my_arg")
    .value_name("MY_ARG")
    .possible_values(&number_strings)
)
(boilerplate)
```

<details>
<summary> Example Output </summary>
<pre>
<code>

error: 'blah' isn't a valid value for '--my_arg <MY_ARG>'
        [possible values: 0, 1, 12, 2]

</code>
</pre>
</details>