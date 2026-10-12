from ragdemo.parse.tiered.probe import ScanSummary, l2_decision, summarize_scan


def test_summary_counts_pages_docs_and_cid() -> None:
    s = summarize_scan(
        {
            "a.pdf": [None, "文字层不足 40 字符"],
            "b.pdf": [None, None, "文字层乱码（cid）"],
            "c.pdf": [None],
        }
    )
    assert (s.docs, s.pages, s.no_text_pages, s.cid_pages, s.docs_with_no_text) == (3, 6, 2, 1, 2)
    assert round(s.page_ratio, 4) == round(2 / 6, 4)
    assert round(s.doc_ratio, 4) == round(2 / 3, 4)


def test_empty_corpus_has_zero_ratios_and_defers_l2() -> None:
    s = summarize_scan({})
    assert s.page_ratio == 0.0 and s.doc_ratio == 0.0
    assert l2_decision(s) == "defer_l2"


def test_l2_is_built_when_either_ratio_reaches_its_threshold() -> None:
    by_docs = ScanSummary(
        docs=100, pages=10000, no_text_pages=100, cid_pages=0, docs_with_no_text=5
    )
    assert l2_decision(by_docs) == "build_l2"  # 文档占比 0.05 达标（页占比只有 0.01）
    by_pages = ScanSummary(docs=100, pages=1000, no_text_pages=20, cid_pages=0, docs_with_no_text=1)
    assert l2_decision(by_pages) == "build_l2"  # 页占比 0.02 达标
    rare = ScanSummary(docs=100, pages=1000, no_text_pages=19, cid_pages=0, docs_with_no_text=4)
    assert l2_decision(rare) == "defer_l2"  # 0.019 / 0.04 都不到门槛
