# Task 14 report (R3)
- Changed: retrieval/types.py (3 optional fields), retrieval/expand.py (LEFT JOIN asof.doc_table; unit hidden when currency not in (None,CNY); caption hidden when can_show_raw=false; absent when no visible grid), tests/retrieval/test_expand.py (+7 db tests), tests/tables/conftest.py (insert_test_grid).
- TDD: 7 new tests RED (AttributeError on missing fields) then GREEN.
- Tests: retrieval/evals/tables/dataapi 416 passed (excluding 3 untracked foreign test files from another task whose modules do not exist yet: test_continuation, test_html_grid, test_page_engines).
- Ranking identity: C1 eval-db4ac94b678d vs baseline eval-aabfd0199d75 metrics identical; table-gate passed. C2 未测.
- Report section appended to docs/10.12-plan/验收记录/表格检索改进-评测报告.md.

## Review fix
Superseded+live versions both visible at historical as_of: expand.py (LATERAL ... ORDER BY table_id DESC LIMIT 1), store._SELECT (ORDER BY table_id DESC LIMIT 1; citation uses it). Tests: store test RED->GREEN; expand tests (both directions) are guards (did not go RED on tiny fixture). C1 rerun eval-2abd807c42c1 identical to B0 per-group/overall; table-gate pass. fact_check.py reads core.doc_table directly (not mine).
