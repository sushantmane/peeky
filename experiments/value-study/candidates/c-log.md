# C candidates examined (in order)

| instance | outcome | note |
|---|---|---|
| micropython__micropython-13569 | accepted (C) | compiler under-counts stack depth in try/finally, clobbering a frame local; optimized build; 3 native-emitter P2P removed (skipped on arm64) |
| valkey-io__valkey-1842 | accepted (D) | NULL c->user of the primary-link client dereferenced by ACL LOAD on a replica; needs primary-replica setup; -O3/LTO build |
| jqlang__jq-2650 | accepted (E) | grammar lacks `Term . [ ]` productions; control slot; note: the report examples already parse at base, only `.a.[]` fails |
| redis__redis-10764 | skipped | category slot full (E: report names the cause and links the line; blockingGenericZpopCommand blocks on argv+1..argc-2 instead of the parsed keys) |
| micropython__micropython-15898 | skipped | category slot full (E: report gives the cause and the fix in mpz_as_str_inpl) |
| valkey-io__valkey-790 | accepted (D) | CLUSTER SHARDS takes slots from the first (failed old primary) node of a shard; needs cluster failover; -O3/LTO build |

Quota full: 4 of 4 accepted (1 E, 3 A-D).
