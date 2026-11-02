Removing `into_string_parsable`
I suggest removing `into_string_parsable`.  Currently it is equivalent to `into_string()`, but with single quotes around strings.  It's used in two places:

- The documentation generator for default values.  Here I suggest using `into_string()` with backtick delimiting, so:
   ```Nushell
   text <string>:  (optional, default: 'a default string')
   rec <record>:  (optional, default: {a: 1})
   lst <list<any>>:  (optional, default: [1, 2, 3])
   ```
   gets changed into:

   ```Nushell
   text <string>:  (optional, default: `a default string`)
   rec <record>:  (optional, default: `{a: 1}`)
   lst <list<any>>:  (optional, default: `[1, 2, 3]`)
    ```

- `help_completions` menu.  Haven't figured out how this one works yet.  I looks similar to the doc generator.