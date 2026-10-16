# Code gold set

Golden queries over Tychora's own source (`src/tychora`) at pinned commit `700df656` (tree `34fa1cac`). Each query in `queries.json` names the files and definitions a good result contains; `cases.template.jsonl` holds the case templates that the test materializes against the ingested repository.

## Running

Export the pinned `src` tree into a directory and point the test at it:

```
TYCHORA_TEST_CODE_CORPUS=/path/to/corpus \
  pytest tests/evaluation/test_code_gold.py -p no:xdist
```

`/path/to/corpus` contains the pinned `src/` directory. Without the variable the test is skipped. Set `TYCHORA_CODE_GOLD_REPORT` to write the mean metrics per variant as JSON. A full run takes about 15 minutes.

## No baseline

`baseline.json` is absent. A whole-repository extraction needs about 19k retained-binding rows and 19.6 MB, which exceeds the evaluation budget (16,384 rows, 16 MiB), so the run fails with `LIMIT_EXCEEDED`. The test is a strict expected failure until repository sources fit the budget; it then reports an unexpected pass, and the baseline can be recorded.

## Informational run

Measured once in a scratch run with both binding limits raised 4x. This is not a baseline.

| Variant | file_recall | file_mrr | file_ndcg | definition_recall |
| --- | --- | --- | --- | --- |
| lexical | 0.8696 | 0.7101 | 0.7469 | 0.0435 |
| lexical_structural | 0.8696 | 0.6848 | 0.7279 | 0.0 |

`definition_recall` near zero is under investigation.
