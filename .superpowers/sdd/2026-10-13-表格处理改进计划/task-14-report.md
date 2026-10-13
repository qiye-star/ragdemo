# Task 14 report (R3)
- Changed: retrieval/types.py (3 optional fields), retrieval/expand.py (LEFT JOIN asof.doc_table; unit hidden when currency not in (None,CNY); caption hidden when can_show_raw=false; absent when no visible grid), tests/retrieval/test_expand.py (+7 db tests), tests/tables/conftest.py (insert_test_grid).
- TDD: 7 new tests RED (AttributeError on missing fields) then GREEN.
- Tests: retrieval/evals/tables/dataapi 416 passed (excluding 3 untracked foreign test files from another task whose modules do not exist yet: test_continuation, test_html_grid, test_page_engines).
- Ranking identity: C1 eval-db4ac94b678d vs baseline eval-aabfd0199d75 metrics identical; table-gate passed. C2 未测.
- Report section appended to docs/10.12-plan/验收记录/表格检索改进-评测报告.md.
