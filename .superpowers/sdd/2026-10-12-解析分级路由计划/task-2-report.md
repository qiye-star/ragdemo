# Task 2 working notes (2026-10-12)
- Runs: docs (22 docs / 131 pages, 168603 in / 82266 out, 165 calls, 0 ERR); research = first 10 of 91 files, copied to a scratchpad dir (runner takes a directory; runner unmodified), 108 pages, 276290 / 136621, 148 calls.
- Extra spend: spot-check re-run (docs 549/555/562/564 + 10 research) 288396 / 142020; false-alarm re-run of 4 pages 22983 / 11586. Total about 1.13M tokens (under the 1.5M guard).
- Verdict: provisional NO-GO. Page failure rate docs 18.3%, research 27.8% (gate 5%); precision median 0.9474 (gate 0.995); coverage median 0.9528 (0.97); table-row recall mean 0.843; recall median 1.0; cost 0.0119 yuan/page at deepseek-flash peak price (deepseek-chat alias unverified; FX 7.2 assumed).
- p0: announcement 68/87 = 0.7816, research_report 78/108 = 0.7222.
- Findings: LLM output is non-deterministic between runs (564 p3 and guoxin p19 flipped). closure_failed p1 is an em-dash false alarm; p19 is a table-cell issue. Many structure_failed pages dropped page-header watermark characters.
- No bug found in runner or parse/local.
- Spot-check material: docs/9.28-plan/验收记录/phase-9-抽查清单.md (+ data/experiments/phase-9-spotcheck-30numbers-2026-10-12.tsv, git-ignored).
- Scratch scripts stay in the session scratchpad (not committed).
