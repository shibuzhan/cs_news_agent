from __future__ import annotations

from app.services.review_feedback import normalize_review_feedback, normalized_review_report


def test_review_feedback_normalizes_legacy_object_items() -> None:
    issues = normalize_review_feedback([
        {"type": "事实核验", "description": "部署方式缺少来源支持"},
        "术语需要解释",
        {"type": "事实核验", "description": "部署方式缺少来源支持"},
        {"type": "无描述"},
    ])

    assert issues == ["事实核验：部署方式缺少来源支持", "术语需要解释"]


def test_review_report_never_keeps_object_values() -> None:
    report = normalized_review_report({"issues": [{"type": "结构", "description": "段落不足"}]})

    assert report["issues"] == ["结构：段落不足"]
