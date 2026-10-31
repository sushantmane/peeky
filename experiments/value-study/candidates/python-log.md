# Python candidates examined (in order)

| instance | outcome | note |
|---|---|---|
| pallets__flask-5014 | accepted (E) | control; Python 3.11; validated base/gold-1/gold-2 |
| sphinx-doc__sphinx-7889 | accepted (E) | control; Python 3.12; validated base/gold-1/gold-2 |
| pylint-dev__pylint-6528 | skipped | category slot full (E: report gives the commands; `_discover_files` never applies the ignore options) |
| pydata__xarray-4075 | skipped | category slot full (E: report itself traces it to `xr.dot` of two bool arrays in `_sum_of_weights`) |
| matplotlib__matplotlib-24149 | skipped | category slot full (E: traceback points at the uncaught StopIteration from `_safe_first_finite` in `_convert_dx`) |
| astropy__astropy-7336 | skipped | category slot full (E: traceback shows `return_.to(...)` called on the None returned under a `-> None` annotation) |
| mwaskom__seaborn-3069 | accepted (B) | Python 3.11; pandas 1.5.3 instead of the spec's 2.0.0 (see bug.yaml); validated base/gold-1/gold-2 |
| psf__requests-5414 | skipped | category slot full (E: report links the `host.startswith('*')` check that misses a leading '.') |
| scikit-learn__scikit-learn-15100 | skipped | category slot full (E: report explains that `strip_accents_unicode` returns NFKD input unchanged) |
| sympy__sympy-13091 | skipped | category slot full (E: report names the `return False` in `Basic.__eq__` to turn into NotImplemented) |
| pytest-dev__pytest-5787 | skipped | Python project doesn't run on 3.11/3.12: pytest 5.1's assertion rewriter fails on 3.11 (`TypeError: required field "lineno" missing from alias`, fixed upstream only in pytest 6.2.5), so its own test suite can't start (would have been D) |
| django__django-12741 | skipped | category slot full (E: requested signature simplification of `execute_sql_flush`, fully specified in the report) |
| sphinx-doc__sphinx-9230 | skipped | category slot full (E: the garbled output shows the field argument split at its first space; `fieldarg.split` in util/docfields.py) |
| pylint-dev__pylint-6903 | skipped | category slot full (E: report traces it to `int(cpu_shares / 1024)` being 0 in `_query_cpu` and proposes the fix) |
| pydata__xarray-3151 | skipped | category slot full (E: the error message leads to the monotonic check in `combine_by_coords`, which loops over all dims instead of the concatenated ones) |
| matplotlib__matplotlib-26113 | skipped | category slot full (E: off-by-one `len(acc) > mincnt` in hexbin's C branch, clear from the report's comparison) |
| astropy__astropy-13236 | skipped | category slot full (E: the report quotes the clause in `Table._convert_data_to_col` to remove) |
| mwaskom__seaborn-3187 | accepted (B) | Python 3.11; pandas 1.5.3 instead of the spec's 2.0.0 (see bug.yaml); validated base/gold-1/gold-2 |
| psf__requests-1766 | skipped | category slot full (E: report says the qop value must be quoted; one format string in `HTTPDigestAuth.build_digest_header`) |
| scikit-learn__scikit-learn-13496 | skipped | category slot full (E: feature request to expose `warm_start` in `IsolationForest.__init__`, fully specified) |
| sympy__sympy-13372 | skipped | category slot full (E: traceback and report point at the missing `else: raise NotImplementedError` in `evalf`) |
| pytest-dev__pytest-7490 | skipped | Python project doesn't run on 3.11/3.12: pytest 6.0's assertion rewriter fails on 3.11 (`TypeError: required field "lineno" missing from alias`), so its test suite can't start (would have been D) |
| django__django-11551 | skipped | category slot full (E: report walks through `_check_list_display_item`'s branches and the commit that broke them) |
| sphinx-doc__sphinx-10466 | skipped | category slot full (E: report locates the duplicate locations in builders/gettext.py's Message/Catalog and suggests de-duplicating) |
| pylint-dev__pylint-4604 | skipped | category slot full (E: `_store_type_annotation_node` handles Name and Subscript but not Attribute annotations) |
| pydata__xarray-6744 | skipped | category slot full (E: `DataArrayRolling.__iter__` computes window bounds without looking at `center`) |
| matplotlib__matplotlib-24570 | skipped | category slot full (E: "top"/"bottom" swapped in `_get_aligned_offsets`) |
| astropy__astropy-13398 | accepted (B) | Python 3.11; also D; validated base/gold-1/gold-2 |
| psf__requests-1142 | skipped | category slot full (E: `prepare_content_length` unconditionally sets Content-Length: 0) |
| scikit-learn__scikit-learn-14496 | skipped | category slot full (E: report pinpoints the float `min_samples` not converted to int and gives the fix) |
| sympy__sympy-20590 | skipped | category slot full (E: report says a parent class stopped defining `__slots__`; the Printable mixin lacks it) |
| pytest-dev__pytest-5840 | skipped | Python project doesn't run on 3.11/3.12: pytest 5.1, same assertion-rewriter failure as pytest-dev__pytest-5787 |
| django__django-14238 | skipped | category slot full (E: traceback leads to `issubclass(..., AutoField)`; `AutoFieldMeta.__subclasscheck__` tests membership instead of subclassing) |
| sphinx-doc__sphinx-7985 | skipped | tests need the network (the linkcheck tests fetch www.google.com and expect its responses); category would be E |
| pylint-dev__pylint-6386 | skipped | category slot full (E: `_preprocess_options` handles `--verbose` but not `-v`, which argparse then expects a value for) |
| pydata__xarray-6599 | accepted (B) | Python 3.11; numpy 1.23.5 for the spec's 1.23.0 (no 3.11 build); validated base/gold-1/gold-2 |
| matplotlib__matplotlib-24627 | skipped | category slot full (E: `Axes.__clear` drops `_children` without unsetting their `.axes`/`.figure`, as the report describes) |
| astropy__astropy-14182 | skipped | category slot full (E: traceback shows `RST.__init__` not accepting `header_rows`; extend it like FixedWidth) |
| psf__requests-2317 | skipped | tests need the network (most FAIL_TO_PASS tests request http://httpbin.org) |
| scikit-learn__scikit-learn-13439 | skipped | category slot full (E: Pipeline lacks `__len__`) |
| sympy__sympy-19637 | skipped | category slot full (E: UnboundLocalError on `kern` in `kernS`, visible from the traceback) |
| pytest-dev__pytest-7432 | skipped | Python project doesn't run on 3.11/3.12: pytest 5.4, same assertion-rewriter failure as pytest-dev__pytest-5787 (category would be E) |
| django__django-16429 | skipped | category slot full (E: report pinpoints the naive `pivot` datetime in `timesince` and gives the fix) |
| sphinx-doc__sphinx-7757 | skipped | category slot full (E: `signature_from_str`'s positional-only loop never assigns defaults) |
| pylint-dev__pylint-4970 | skipped | category slot full (E: requested behaviour for `min-similarity-lines=0` in `Similar.run`) |
| pydata__xarray-3305 | skipped | category slot full (E: `keep_attrs` is not passed from `Dataset.quantile` to `Variable.quantile`) |
| matplotlib__matplotlib-24870 | skipped | category slot full (E: feature; `_contour_args` casts z to float64 before the level defaults are chosen) |
| astropy__astropy-14365 | skipped | category slot full (E: the QDP line regex is case-sensitive, per the report) |
| psf__requests-6028 | accepted (B) | Python 3.12; also D; eval.sh clears a /tmp cache file (see bug.yaml); validated base/gold-1/gold-2 |
| scikit-learn__scikit-learn-14053 | skipped | category slot full (E: IndexError in `export_text`'s `feature_names[i] for i in tree_.feature`, where leaves hold TREE_UNDEFINED) |
| sympy__sympy-17139 | skipped | category slot full (E: traceback ends at `rv.exp < 0` on a complex exponent in fu.py's `_TR56`) |
| pytest-dev__pytest-7205 | skipped | Python project doesn't run on 3.11/3.12: pytest 5.4, same assertion-rewriter failure as pytest-dev__pytest-5787 (category would be E) |
| django__django-11206 | skipped | category slot full (E: report names the hard-coded exponent cut-off in `numberformat.format`) |
| sphinx-doc__sphinx-10673 | skipped | category slot full (E: feature; the warning text leads to `TocTree.parse_content` and the toctree adapter) |
| pylint-dev__pylint-8898 | skipped | category slot full (E: report explains that the regex-list option is split on every comma by `_csv_transformer`) |
| pydata__xarray-6721 | skipped | category slot full (E: the report's traceback shows `get_chunksizes` touching `v.data`, which loads lazy arrays) |
| matplotlib__matplotlib-24970 | skipped | category slot full (E: NumPy's warnings name the lines; integer input isn't cast to int before the over/under/bad indices are stored) |
| astropy__astropy-14096 | skipped | category slot full (E: traceback ends in `SkyCoord.__getattr__`, which replaces the property's AttributeError with its own message) |
| psf__requests-1921 | skipped | tests need the network (FAIL_TO_PASS tests request http://httpbin.org); category would be E |
| scikit-learn__scikit-learn-12585 | skipped | category slot full (E: report gives the failing `clone` branch and the fix) |
| sympy__sympy-18698 | skipped | category slot full (E: `_symbolic_factor_list` handles each factor of the product separately and never merges factors of equal multiplicity for sqf) |
| pytest-dev__pytest-5262 | skipped | Python project doesn't run on 3.11/3.12: pytest 4.5, same assertion-rewriter failure as pytest-dev__pytest-5787 (category would be E) |
| django__django-11087 | accepted (D) | Python 3.11; also B; parse.py maps Python 3.11 unittest names to SWE-bench's (see bug.yaml); validated base/gold-1/gold-2 |
| sphinx-doc__sphinx-10614 | accepted (D) | Python 3.12; graphviz from apt; validated base/gold-1/gold-2 |
