Implement JSON Pointer on top of On Demand
[Currently our JSON Pointer API assumes a DOM tree](https://github.com/simdjson/simdjson/blob/c5def8f7060a7e9519c7f38e2c4ddf2dc784d6a8/doc/basics.md#json-pointer), but it is obvious that we can do it with On Demand.


If the pointer points at an array or an object, you'd like to be able to do...

```C++
    auto doc = parser.iterate(json);
    for (auto x : doc.at_pointer(insert json pointer expression)) {
     
    }
```

It could have substantial performance gains.

[The `at_pointer` functions in our DOM API have a rather clean implementation](https://github.com/simdjson/simdjson/blob/95b4870e20be5f97d9dcf63b23b1c6f520c366c1/include/simdjson/dom/object.h). It is then a matter of 'porting' them to the On Demand front-end. There will be some fun challenges.