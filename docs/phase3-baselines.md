# Phase 3: both baselines through the harness

RC1-631. Development entry point, profiling slice. Criteria sha256 `da51e19e9bcae985...`, load model `5a93d123dac4c3d6...`, config space `90b422ce74b4715a...`. Run: https://github.com/snacksnack/poc-benchmark-agent/actions/runs/42388461508.

Every number is **performance at N concurrent sessions in a closed loop**, not a sustained arrival rate (ADR 0012). Latency is **net**: client latency minus the run's median probe round trip (criteria method, R9). Level 1 is diagnostic only. Mix-level numbers are reported, never scored (R12).

24 scored runs, 0 rejected by the network or server checks, 12 cells. Record chain verified: 25 lines.

## Network gate

Preflight: probe p50 18.6 ms, p99 - p50 0.4 ms over 300 round trips: accepted. Rule: p50 <= 100 ms and p99 - p50 <= 15 ms.
Scored runs: probe p50 18.2-25.9 ms, p99 - p50 1.9-12.2 ms.

## Cells

| Config | CU | Level | Cache state | Reps | Aborted | OK qps (median) | Mix p99 net (reported) | Cost per successful query |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| default | 2 | 1 (diag.) | post-restart-with-eviction | 2 | 0 | 0.1 | 3,806.7 | $6.09e-04 |
| default | 2 | 1 (diag.) | warm | 2 | 0 | 0.1 | 1,967.5 | $4.91e-04 |
| default | 2 | 8 | post-restart-with-eviction | 2 | 0 | 0.8 | 3,157.1 | $7.33e-05 |
| default | 2 | 8 | warm | 2 | 0 | 0.9 | 2,704.2 | $6.85e-05 |
| default | 2 | 32 | post-restart-with-eviction | 2 | 0 | 2.2 | 4,924.9 | $2.73e-05 |
| default | 2 | 32 | warm | 2 | 0 | 2.2 | 4,944.3 | $2.73e-05 |
| naive | 2 | 1 (diag.) | post-restart-with-eviction | 2 | 0 | 2.2 | 2,256.7 | $2.66e-05 |
| naive | 2 | 1 (diag.) | warm | 2 | 0 | 2.0 | 2,461.9 | $2.98e-05 |
| naive | 2 | 8 | post-restart-with-eviction | 2 | 0 | 7.0 | 4,246.9 | $8.40e-06 |
| naive | 2 | 8 | warm | 2 | 0 | 6.9 | 4,149.7 | $8.52e-06 |
| naive | 2 | 32 | post-restart-with-eviction | 2 | 0 | 12.8 | 4,496.9 | $4.60e-06 |
| naive | 2 | 32 | warm | 2 | 0 | 12.7 | 4,477.2 | $4.64e-06 |

## Per shape

Net ms. `*` = tail not supported (ADR 0018: 100 observations from 10 distinct instances beyond the percentile). Spread = (max - min) / median of the per-repetition p99.

| Config | Level | Cache state | Shape | Obs | Errors + timeouts | p50 | p95 | p99 [95% CI] | p99 spread |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | ---: |
| default | 1 | post-restart-with-eviction | s01 | 68 | 100.0% **over** | - | - | - | - |
| default | 1 | post-restart-with-eviction | s02 | 28 | 0.0% | 435.2* | 3,801.0* | 3,806.7* [3,800.6, 3,806.7] | 49.6% |
| default | 1 | post-restart-with-eviction | s03 | 20 | 100.0% **over** | - | - | - | - |
| default | 1 | post-restart-with-eviction | s04 | 16 | 0.0% | 187.2* | 1,657.0* | 1,657.0* [1,106.4, 1,657.0] | 39.9% |
| default | 1 | post-restart-with-eviction | s05 | 8 | 100.0% **over** | - | - | - | - |
| default | 1 | post-restart-with-eviction | s06 | 10 | 20.0% **over** | 430.3* | 2,011.2* | 2,011.2* [1,809.0, 2,011.2] | 28.1% |
| default | 1 | post-restart-with-eviction | s07 | 8 | 100.0% **over** | - | - | - | - |
| default | 1 | post-restart-with-eviction | s08 | 6 | 0.0% | 2.0* | 26.7* | 26.7* [16.6, 26.7] | 46.4% |
| default | 1 | post-restart-with-eviction | s09 | 6 | 100.0% **over** | - | - | - | - |
| default | 1 | warm | s01 | 70 | 100.0% **over** | - | - | - | - |
| default | 1 | warm | s02 | 38 | 0.0% | 419.4* | 1,965.9* | 1,967.5* [1,961.7, 1,967.5] | 0.3% |
| default | 1 | warm | s03 | 20 | 100.0% **over** | - | - | - | - |
| default | 1 | warm | s04 | 12 | 0.0% | 19.6* | 529.9* | 529.9* [459.9, 529.9] | 14.1% |
| default | 1 | warm | s05 | 4 | 100.0% **over** | - | - | - | - |
| default | 1 | warm | s06 | 12 | 16.7% **over** | 961.7* | 1,849.5* | 1,849.5* [1,773.6, 1,849.5] | 4.2% |
| default | 1 | warm | s07 | 8 | 100.0% **over** | - | - | - | - |
| default | 1 | warm | s08 | 12 | 0.0% | 0.0* | 14.2* | 14.2* [13.3, 14.2] | 6.5% |
| default | 1 | warm | s09 | 6 | 100.0% **over** | - | - | - | - |
| default | 8 | post-restart-with-eviction | s01 | 553 | 100.0% **over** | - | - | - | - |
| default | 8 | post-restart-with-eviction | s02 | 213 | 0.5% | 1,158.7 | 2,349.9* | 2,700.2* [2,568.9, 3,723.5] | 10.3% |
| default | 8 | post-restart-with-eviction | s03 | 130 | 100.0% **over** | - | - | - | - |
| default | 8 | post-restart-with-eviction | s04 | 154 | 1.3% **over** | 66.2* | 1,448.3* | 3,561.1* [2,855.9, 4,348.3] | 19.9% |
| default | 8 | post-restart-with-eviction | s05 | 61 | 100.0% **over** | - | - | - | - |
| default | 8 | post-restart-with-eviction | s06 | 64 | 12.5% **over** | 673.7* | 1,630.3* | 4,285.8* [1,531.0, 4,285.8] | 89.8% |
| default | 8 | post-restart-with-eviction | s07 | 76 | 100.0% **over** | - | - | - | - |
| default | 8 | post-restart-with-eviction | s08 | 72 | 13.9% **over** | 0.0* | 17.2* | 28.0* [17.2, 28.0] | 46.1% |
| default | 8 | post-restart-with-eviction | s09 | 41 | 100.0% **over** | - | - | - | - |
| default | 8 | warm | s01 | 535 | 100.0% **over** | - | - | - | - |
| default | 8 | warm | s02 | 238 | 0.0% | 1,291.6 | 2,397.9* | 2,715.3* [2,597.9, 2,823.0] | 11.3% |
| default | 8 | warm | s03 | 128 | 100.0% **over** | - | - | - | - |
| default | 8 | warm | s04 | 143 | 0.0% | 68.7* | 1,283.3* | 3,274.4* [1,735.3, 4,842.4] | 38.6% |
| default | 8 | warm | s05 | 68 | 100.0% **over** | - | - | - | - |
| default | 8 | warm | s06 | 66 | 9.1% **over** | 728.1* | 1,286.2* | 1,713.7* [1,286.2, 1,713.7] | 13.2% |
| default | 8 | warm | s07 | 79 | 100.0% **over** | - | - | - | - |
| default | 8 | warm | s08 | 85 | 11.8% **over** | 2.4* | 22.2* | 28.2* [22.2, 28.2] | 19.5% |
| default | 8 | warm | s09 | 41 | 100.0% **over** | - | - | - | - |
| default | 32 | post-restart-with-eviction | s01 | 1,788 | 100.0% **over** | - | - | - | - |
| default | 32 | post-restart-with-eviction | s02 | 722 | 67.9% **over** | 3,366.7 | 4,929.7* | 4,982.9* [4,963.8, 4,990.2] | 0.1% |
| default | 32 | post-restart-with-eviction | s03 | 494 | 100.0% **over** | - | - | - | - |
| default | 32 | post-restart-with-eviction | s04 | 551 | 0.0% | 100.9 | 828.2* | 2,157.2* [1,354.6, 4,921.9] | 3.2% |
| default | 32 | post-restart-with-eviction | s05 | 263 | 100.0% **over** | - | - | - | - |
| default | 32 | post-restart-with-eviction | s06 | 281 | 4.3% **over** | 2,990.2 | 4,380.5* | 4,571.3* [4,499.5, 4,739.0] | 0.3% |
| default | 32 | post-restart-with-eviction | s07 | 235 | 100.0% **over** | - | - | - | - |
| default | 32 | post-restart-with-eviction | s08 | 287 | 16.4% **over** | 0.6 | 19.0* | 29.9* [21.6, 46.3] | 30.3% |
| default | 32 | post-restart-with-eviction | s09 | 151 | 100.0% **over** | - | - | - | - |
| default | 32 | warm | s01 | 1,755 | 100.0% **over** | - | - | - | - |
| default | 32 | warm | s02 | 754 | 64.3% **over** | 3,509.5 | 4,943.4* | 4,995.6* [4,976.1, 4,999.4] | 0.7% |
| default | 32 | warm | s03 | 491 | 100.0% **over** | - | - | - | - |
| default | 32 | warm | s04 | 528 | 0.2% | 109.4 | 929.5* | 1,731.7* [1,464.9, 2,949.1] | 26.7% |
| default | 32 | warm | s05 | 260 | 100.0% **over** | - | - | - | - |
| default | 32 | warm | s06 | 263 | 3.0% **over** | 2,942.7 | 4,159.1* | 4,757.7* [4,402.0, 4,908.5] | 4.1% |
| default | 32 | warm | s07 | 257 | 100.0% **over** | - | - | - | - |
| default | 32 | warm | s08 | 277 | 11.6% **over** | 4.8 | 25.0* | 38.0* [32.2, 41.0] | 9.5% |
| default | 32 | warm | s09 | 167 | 100.0% **over** | - | - | - | - |
| naive | 1 | post-restart-with-eviction | s01 | 556 | 0.0% | 8.4 | 30.8* | 64.0* [43.3, 91.9] | 18.6% |
| naive | 1 | post-restart-with-eviction | s02 | 228 | 0.0% | 464.1 | 537.4* | 2,338.1* [1,687.0, 2,451.5] | 0.3% |
| naive | 1 | post-restart-with-eviction | s03 | 128 | 23.4% **over** | 43.5* | 1,812.0* | 4,315.9* [2,180.0, 4,315.9] | 14.8% |
| naive | 1 | post-restart-with-eviction | s04 | 114 | 0.0% | 14.5* | 130.7* | 415.4* [154.2, 425.8] | 7.7% |
| naive | 1 | post-restart-with-eviction | s05 | 94 | 14.9% **over** | 208.1* | 2,780.1* | 4,341.2* [3,135.6, 4,341.2] | 5.9% |
| naive | 1 | post-restart-with-eviction | s06 | 86 | 0.0% | 11.3* | 177.6* | 213.4* [179.4, 213.4] | 1.2% |
| naive | 1 | post-restart-with-eviction | s07 | 62 | 0.0% | 1,110.2* | 1,369.5* | 1,520.4* [1,369.5, 1,520.4] | 18.5% |
| naive | 1 | post-restart-with-eviction | s08 | 74 | 13.5% **over** | 2.0* | 30.6* | 37.9* [30.6, 37.9] | 5.1% |
| naive | 1 | post-restart-with-eviction | s09 | 40 | 0.0% | 280.6* | 1,702.0* | 2,256.7* [1,702.0, 2,256.7] | 6.2% |
| naive | 1 | warm | s01 | 517 | 0.0% | 7.7 | 28.1* | 50.9* [36.3, 80.5] | 0.8% |
| naive | 1 | warm | s02 | 200 | 0.0% | 459.0 | 549.9* | 2,401.3* [2,315.2, 2,479.4] | 3.0% |
| naive | 1 | warm | s03 | 106 | 26.4% **over** | 33.6* | 2,967.5* | 4,284.6* [2,967.5, 4,284.6] | 0.8% |
| naive | 1 | warm | s04 | 98 | 0.0% | 13.0* | 96.1* | 144.2* [108.2, 144.2] | 12.8% |
| naive | 1 | warm | s05 | 90 | 15.6% **over** | 270.9* | 3,345.1* | 4,655.1* [3,345.1, 4,655.1] | 6.4% |
| naive | 1 | warm | s06 | 69 | 0.0% | 11.7* | 187.7* | 257.8* [187.7, 257.8] | 18.6% |
| naive | 1 | warm | s07 | 58 | 0.0% | 1,231.8* | 1,463.7* | 1,617.3* [1,436.1, 1,617.3] | 9.6% |
| naive | 1 | warm | s08 | 60 | 16.7% **over** | 0.9* | 10.0* | 35.8* [10.0, 35.8] | 9.2% |
| naive | 1 | warm | s09 | 40 | 5.0% **over** | 306.7* | 2,212.4* | 2,364.6* [1,908.8, 2,364.6] | 6.7% |
| naive | 8 | post-restart-with-eviction | s01 | 1,903 | 0.0% | 32.4 | 265.6* | 624.9* [496.9, 725.1] | 17.2% |
| naive | 8 | post-restart-with-eviction | s02 | 757 | 0.0% | 1,385.3 | 3,540.6* | 4,531.1* [4,368.2, 4,930.2] | 0.4% |
| naive | 8 | post-restart-with-eviction | s03 | 442 | 32.6% **over** | 86.6 | 3,611.9* | 4,861.7* [4,396.1, 4,931.6] | 1.2% |
| naive | 8 | post-restart-with-eviction | s04 | 489 | 0.0% | 76.1 | 402.6* | 967.4* [631.0, 1,942.6] | 6.9% |
| naive | 8 | post-restart-with-eviction | s05 | 275 | 33.1% **over** | 1,132.6* | 4,631.4* | 4,962.9* [4,756.9, 4,993.2] | 0.6% |
| naive | 8 | post-restart-with-eviction | s06 | 267 | 0.0% | 25.6 | 1,028.8* | 2,009.4* [1,446.3, 3,716.3] | 29.1% |
| naive | 8 | post-restart-with-eviction | s07 | 242 | 91.7% **over** | 443.7* | 3,617.0* | 4,249.3* [3,308.8, 4,249.3] | 43.9% |
| naive | 8 | post-restart-with-eviction | s08 | 239 | 15.9% **over** | 3.7 | 22.4* | 29.3* [27.2, 72.7] | 47.8% |
| naive | 8 | post-restart-with-eviction | s09 | 128 | 33.6% **over** | 839.5* | 3,999.9* | 4,870.2* [4,246.9, 4,870.2] | 13.7% |
| naive | 8 | warm | s01 | 1,865 | 0.0% | 32.6 | 261.8* | 521.3* [468.7, 680.2] | 11.7% |
| naive | 8 | warm | s02 | 754 | 0.3% | 1,408.3 | 3,523.9* | 4,475.6* [4,285.0, 4,832.5] | 0.6% |
| naive | 8 | warm | s03 | 429 | 33.3% **over** | 101.4 | 1,358.4* | 4,577.7* [2,737.0, 4,928.3] | 0.1% |
| naive | 8 | warm | s04 | 463 | 0.0% | 84.1 | 401.0* | 777.9* [594.5, 2,551.0] | 7.0% |
| naive | 8 | warm | s05 | 312 | 36.5% **over** | 757.4* | 3,769.2* | 4,882.5* [4,303.3, 4,907.2] | 13.1% |
| naive | 8 | warm | s06 | 275 | 0.0% | 27.4 | 1,065.0* | 1,493.9* [1,212.8, 2,200.2] | 2.3% |
| naive | 8 | warm | s07 | 236 | 90.7% **over** | 1,208.6* | 4,618.4* | 4,654.9* [4,393.6, 4,654.9] | 7.2% |
| naive | 8 | warm | s08 | 236 | 14.8% **over** | 3.6 | 20.2* | 34.2* [27.0, 63.2] | 23.4% |
| naive | 8 | warm | s09 | 127 | 34.6% **over** | 807.3* | 4,237.1* | 4,544.2* [4,283.8, 4,544.2] | 0.5% |
| naive | 32 | post-restart-with-eviction | s01 | 4,114 | 0.2% | 151.3 | 1,378.9 | 2,545.9* [2,359.1, 2,938.9] | 1.4% |
| naive | 32 | post-restart-with-eviction | s02 | 1,635 | 77.4% **over** | 3,937.5 | 4,610.2* | 4,880.3* [4,815.0, 4,964.6] | 0.9% |
| naive | 32 | post-restart-with-eviction | s03 | 1,046 | 35.0% **over** | 102.3 | 2,048.1* | 3,631.3* [3,027.7, 4,681.5] | 13.5% |
| naive | 32 | post-restart-with-eviction | s04 | 1,099 | 0.1% | 313.4 | 1,162.6* | 1,968.1* [1,583.9, 2,304.5] | 14.7% |
| naive | 32 | post-restart-with-eviction | s05 | 589 | 74.2% **over** | 2,493.9* | 4,569.7* | 4,921.1* [4,704.1, 4,942.6] | 5.4% |
| naive | 32 | post-restart-with-eviction | s06 | 655 | 0.9% | 65.3 | 3,075.3* | 4,592.6* [4,293.1, 4,902.8] | 7.1% |
| naive | 32 | post-restart-with-eviction | s07 | 496 | 97.8% **over** | 1,086.2* | 4,891.8* | 4,891.8* [1,930.6, 4,891.8] | 104.0% |
| naive | 32 | post-restart-with-eviction | s08 | 623 | 16.2% **over** | 3.0 | 31.8* | 51.5* [43.5, 103.7] | 44.3% |
| naive | 32 | post-restart-with-eviction | s09 | 317 | 72.6% **over** | 2,506.0* | 4,690.0* | 4,941.8* [4,727.5, 4,941.8] | 4.4% |
| naive | 32 | warm | s01 | 4,112 | 0.2% | 150.2 | 1,366.0 | 2,807.5* [2,354.2, 3,392.7] | 6.9% |
| naive | 32 | warm | s02 | 1,690 | 76.9% **over** | 4,050.2 | 4,567.1* | 4,913.2* [4,669.8, 4,967.6] | 6.4% |
| naive | 32 | warm | s03 | 963 | 33.0% **over** | 167.4 | 2,185.2* | 3,573.2* [3,080.9, 4,465.9] | 9.9% |
| naive | 32 | warm | s04 | 1,034 | 0.0% | 305.3 | 1,075.3* | 1,418.3* [1,293.6, 1,642.1] | 9.3% |
| naive | 32 | warm | s05 | 585 | 74.5% **over** | 2,889.8* | 4,757.9* | 4,947.7* [4,818.2, 4,983.2] | 0.7% |
| naive | 32 | warm | s06 | 648 | 0.6% | 68.0 | 3,497.7* | 4,535.0* [4,300.5, 4,838.9] | 5.7% |
| naive | 32 | warm | s07 | 488 | 97.3% **over** | 1,183.2* | 3,652.3* | 3,652.3* [2,165.3, 3,652.3] | 81.8% |
| naive | 32 | warm | s08 | 634 | 14.5% **over** | 5.4 | 38.1* | 62.7* [52.3, 322.7] | 2.6% |
| naive | 32 | warm | s09 | 327 | 70.9% **over** | 2,270.7* | 4,778.6* | 4,914.8* [4,796.4, 4,914.8] | 2.4% |

## Cache evidence

Named for the procedure (ADR 0013). Ratios are over the measured run; `post-restart-with-eviction` also shows the restart and the filler scan.

| Config | Level | Cache state | File-cache hit ratio per rep | Restart to first query (s) | Filler scan (s) |
| --- | ---: | --- | --- | --- | --- |
| default | 1 | post-restart-with-eviction | 6.2%, 11.8% | 12.0, 42.9 | 31, 48 |
| default | 1 | warm | 7.3%, 7.0% | - | - |
| default | 8 | post-restart-with-eviction | 64.7%, 64.2% | 18.4, 13.8 | 19, 29 |
| default | 8 | warm | 67.8%, 70.0% | - | - |
| default | 32 | post-restart-with-eviction | 89.2%, 88.8% | 13.9, 12.1 | 31, 27 |
| default | 32 | warm | 90.1%, 89.6% | - | - |
| naive | 1 | post-restart-with-eviction | 81.0%, 81.4% | 16.3, 11.7 | 61, 31 |
| naive | 1 | warm | 82.0%, 82.4% | - | - |
| naive | 8 | post-restart-with-eviction | 92.9%, 93.0% | 13.9, 9.7 | 31, 30 |
| naive | 8 | warm | 92.9%, 93.1% | - | - |
| naive | 32 | post-restart-with-eviction | 97.3%, 97.3% | 11.6, 11.7 | 31, 31 |
| naive | 32 | warm | 97.4%, 97.2% | - | - |

## Cost

Four lines, never summed (ticket).

- **Benchmark execution:** $1.07 for 10.05 CU-h held by this job. Neon-reported compute seconds per branch: {'p3-default-20261107T155225Z': 13168, 'p3-naive-20261107T155225Z': 12888}.

| Config | Index build | Index storage / month | Projected operating / month | Basis |
| --- | ---: | ---: | ---: | --- |
| default | $0.000 (0 s) | $0.00 | $158.60 | 2 CU always on x 730 h + 10.98 GB storage (indexes included, filler excluded) |
| naive | $0.010 (84 s) | $0.99 | $159.59 | 2 CU always on x 730 h + 13.80 GB storage (indexes included, filler excluded) |

