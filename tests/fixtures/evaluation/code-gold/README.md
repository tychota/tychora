# Code gold set

Golden queries over Tychora's own source (`src/tychora`) at pinned commit `700df656` (tree `34fa1cac`). Each query in `queries.json` names the files and definitions a good result contains; `cases.template.jsonl` holds the case templates that the test materializes against the ingested repository.

## Running

Export the pinned `src` tree into a directory and point the test at it:

```
git archive 700df656 src/tychora | tar -x -C /path/to/corpus
TYCHORA_TEST_CODE_CORPUS=/path/to/corpus \
  pytest tests/evaluation/test_code_gold.py -p no:xdist
```

`/path/to/corpus` contains the pinned `src/` directory. Without the variable the test is skipped. Set `TYCHORA_CODE_GOLD_REPORT` to write the mean metrics per variant as JSON. A full run takes about 15 minutes.

## Baseline

`baseline.json` holds the mean metrics per variant over the 23 test cases, measured offline with the `code` profile. Nightly gate G22 compares the run's report against the recorded values in `quality/baseline.json`.

| Variant | file_recall | file_mrr | file_ndcg | definition_recall |
| --- | --- | --- | --- | --- |
| lexical | 0.8696 | 0.7101 | 0.7469 | 0.0435 |
| lexical_structural | 0.8696 | 0.6848 | 0.7279 | 0.0 |

The repository is one extraction of about 19,300 retained rows and 30 MB of estimated payload, above the evaluation's retained-binding budget (16,384 rows, 16 MiB). Locator verification therefore binds only the file a code citation names, at most 646 rows for the largest file. The budget bounds the bindings held at once: when it is full the least recently used file is released and hydrated again if it is cited later. One run cites 68 distinct files, 11,234 rows and 17.6 MB in total, which is why the release is needed.

## definition_recall

`definition_recall` counts a definition when a returned segment carries the `code_symbol` record of a matched unit with an expected `definition_key`. The cases ask for `max_tokens` 30000, and the test's token counter counts one token per character, so the response budget trims optional records: of the 147 `code_symbol` records returned by the lexical variant, 143 are replaced by `{"omitted_budget": true}` stubs that carry no `definition_key`. The metric cannot see those definitions.

- Records trimmed by the output budget explain the near-zero value. If the record of every matched symbol unit were kept, the per-case mean would be 0.3696 (lexical) and 0.1522 (lexical_structural): 13 of the 33 expected symbol units (lexical) and 6 (lexical_structural) are among the matched units.
- Matches on non-symbol units contribute little: the lexical variant matches 95 symbol units and 12 code chunks in total.
- Key drift is not a cause: every returned record's `definition_key` equals the stored one.

Trimmed `code_symbol` records keep `definition_key`, `qualified_name` and `relative_path` in their stub (`retrieval/response.py`, `budget_stub`), so the metric sees every definition a result names; the values measured after this change are in the table above.

## Search time

One search over the gold workspace takes about 12 s (median, maximum 21 s over the 46 searches of one run, both variants), measured on a developer laptop. This is information only; the query path gates it.
