#!/usr/bin/env python3
"""Regression contracts for dynamic, localized visual existence queries."""

from online_agent import VisualReasoner


ONE_PIXEL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def candidate_response(camera_name: str) -> dict:
    return {
        "relevance": 1.0,
        "business_policy": "OBSERVATION_ONLY",
        "target_observed": False,
        # This deliberately mirrors the malformed absence response in the
        # reported bad case.  It must trigger a second target-localization pass.
        "evidence_type": "ABSENCE",
        "status": "NEGATIVE",
        "conclusion": "画面中未发现用户询问的目标。",
        "confidence": 0.99,
        "observations": [],
        "exclusions": [],
        "camera_name": camera_name,
    }


def run_red_clothing_regression():
    reasoner = VisualReasoner({"api_key": "test", "model": "smoke-vlm"})
    question = "帮我看下当前镜头画面中是否出现穿红色衣服的人"
    review_calls = []

    def fake_request(system, content, max_tokens=512):
        content_text = str(content)
        camera_name = next((f"展厅{index}" for index in range(1, 8) if f"展厅{index}" in content_text), "")
        if "候选镜头分析器" in system:
            return candidate_response(camera_name)
        if "视觉目标复核器" in system:
            review_calls.append(camera_name)
            if camera_name == "展厅2":
                return {
                    "business_policy": "OBSERVATION_ONLY",
                    "target_observed": True,
                    "evidence_type": "DIRECT_VISUAL",
                    "status": "POSITIVE",
                    "conclusion": "左侧展台旁一名人员穿红色上衣。",
                    "confidence": 0.95,
                    "target_evidence": [
                        {
                            "subject": "人员",
                            "target": "穿红色衣服的人",
                            "attributes": {"上衣颜色": "红色"},
                            "constraint_results": [
                                {"constraint": "对象类别", "expected": "人员", "observed": "人员", "status": "MATCH"},
                                {"constraint": "上衣颜色", "expected": "红色", "observed": "红色", "status": "MATCH"},
                            ],
                            "relation": "穿着",
                            "matches_query": True,
                            "location": "画面左侧展台旁",
                            "bbox_1000": [260, 160, 430, 820],
                            "confidence": 0.95,
                        }
                    ],
                    "absence_evidence": {},
                    "observations": ["画面左侧展台旁一名人员穿红色上衣。"],
                    "exclusions": [],
                }
            return {
                "business_policy": "OBSERVATION_ONLY",
                "target_observed": False,
                "evidence_type": "ABSENCE",
                "status": "NEGATIVE",
                "conclusion": "已检查当前画面中可见人员，未发现红色上衣。",
                "confidence": 0.88,
                "target_evidence": [],
                "absence_evidence": {"coverage": "FULL", "inspected_subject_count": 2, "reason": "逐一检查全部可见人员的上衣颜色"},
                "observations": [],
                "exclusions": [],
            }
        if "视觉颜色盲审器" in system:
            assert "红色" not in system
            return {
                "candidates": [
                    {"candidate_index": 1, "subject": "人员上衣", "dominant_color": "红色", "usable": True, "confidence": 0.96, "reason": "彩色画面可辨"}
                ]
            }
        # The deterministic evidence merge must win even if this output is
        # internally contradictory, as occurred in the production bad case.
        return {
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": False,
            "evidence_type": "ABSENCE",
            "status": "NEGATIVE",
            "conclusion": "画面中未发现穿红衣服的人。",
            "confidence": 0.99,
            "selected_camera_names": [f"展厅{index}" for index in range(1, 8)],
            "observations": [],
            "exclusions": [],
        }

    reasoner._request_json = fake_request
    result = reasoner.analyze(
        question,
        [{"camera_name": f"展厅{index}", "snapshot_url": ONE_PIXEL} for index in range(1, 8)],
    )
    assert result["status"] == "POSITIVE"
    assert result["target_observed"] is True
    assert result["selected_camera_names"] == ["展厅2"]
    assert result["target_evidence"][0]["camera_name"] == "展厅2"
    assert result["target_evidence"][0]["attributes"]["上衣颜色"] == "红色"
    assert len(review_calls) == 7
    assert "定位到" in result["conclusion"]


def run_open_vocabulary_backpack_regression():
    reasoner = VisualReasoner({"api_key": "test", "model": "smoke-vlm"})
    question = "画面中是否有背帆布双肩包的人"

    def fake_request(system, content, max_tokens=512):
        if "视觉判断器" in system:
            return {
                "business_policy": "OBSERVATION_ONLY",
                "target_observed": False,
                "evidence_type": "ABSENCE",
                "status": "NEGATIVE",
                "conclusion": "未发现目标。",
                "confidence": 0.9,
            }
        assert "视觉目标复核器" in system
        return {
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": True,
            "evidence_type": "DIRECT_VISUAL",
            "status": "POSITIVE",
            "conclusion": "右下角人员背着帆布双肩包。",
            "confidence": 0.92,
            "target_evidence": [
                {
                    "subject": "人员",
                    "target": "帆布双肩包",
                    "attributes": {"款式": "双肩包", "材质": "帆布"},
                    "constraint_results": [
                        {"constraint": "对象类别", "expected": "背包", "observed": "背包", "status": "MATCH"},
                        {"constraint": "款式", "expected": "双肩包", "observed": "双肩包", "status": "MATCH"},
                        {"constraint": "材质", "expected": "帆布", "observed": "帆布", "status": "MATCH"},
                    ],
                    "relation": "背着",
                    "matches_query": True,
                    "location": "画面右下角",
                    "bbox_1000": [700, 420, 920, 940],
                    "confidence": 0.92,
                }
            ],
            "absence_evidence": {},
            "observations": [],
            "exclusions": [],
        }

    reasoner._request_json = fake_request
    result = reasoner.analyze(question, [{"camera_name": "入口镜头", "snapshot_url": ONE_PIXEL}])
    assert result["status"] == "POSITIVE"
    assert result["target_observed"] is True
    assert result["target_evidence"][0]["attributes"]["材质"] == "帆布"
    assert result["query_spec"]["requires_localized_evidence"] is True


def run_weak_absence_becomes_uncertain_regression():
    result = VisualReasoner()._normalize_result(
        "画面中是否存在一个戴蓝色帽子的顾客",
        [{"camera_name": "入口镜头", "snapshot_url": ONE_PIXEL}],
        {
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": False,
            "evidence_type": "ABSENCE",
            "status": "NEGATIVE",
            "conclusion": "未发现戴蓝色帽子的顾客。",
            "confidence": 0.99,
            "selected_camera_names": ["入口镜头"],
        },
        0,
    )
    assert result["status"] == "UNCERTAIN"
    assert result["target_observed"] is None
    assert "不能将未检出当作不存在" in result["business_reason"]


def run_complete_absence_regression():
    result = VisualReasoner()._normalize_result(
        "画面中是否存在背黄色斜挎包的人",
        [{"camera_name": "入口镜头", "snapshot_url": ONE_PIXEL}],
        {
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": False,
            "evidence_type": "ABSENCE",
            "status": "NEGATIVE",
            "conclusion": "已逐一检查可见人员，未发现背黄色斜挎包的人。",
            "confidence": 0.86,
            "selected_camera_names": ["入口镜头"],
            "target_evidence": [],
            "absence_evidence": {"coverage": "FULL", "inspected_subject_count": 3, "reason": "所有可见人员及携带物均清晰可辨"},
        },
        0,
    )
    assert result["status"] == "NEGATIVE"
    assert result["target_observed"] is False


def run_attribute_false_positive_review_regression():
    """A model-labelled MATCH with ambiguous observed colour must not pass."""
    reasoner = VisualReasoner({"api_key": "test", "model": "smoke-vlm"})
    calls = []

    def fake_request(system, content, max_tokens=512):
        calls.append(system)
        if "视觉判断器" in system:
            return {
                "business_policy": "OBSERVATION_ONLY",
                "target_observed": True,
                "evidence_type": "DIRECT_VISUAL",
                "status": "POSITIVE",
                "conclusion": "画面中存在黑色沙发。",
                "confidence": 1,
                "target_evidence": [
                    {
                        "subject": "沙发",
                        "target": "黑色沙发",
                        "attributes": {},
                        "matches_query": True,
                        "location": "画面中央",
                        "bbox_1000": [100, 100, 900, 900],
                        "confidence": 0,
                    }
                ],
            }
        if "视觉颜色盲审器" in system:
            assert "黑色" not in system
            return {
                "candidates": [
                    {
                        "candidate_index": 1,
                        "subject": "沙发",
                        "dominant_color": "深灰色",
                        "usable": True,
                        "confidence": 0.93,
                        "reason": "彩色画面可辨",
                    }
                ]
            }
        assert "视觉目标复核器" in system
        assert "constraint_results" in system
        return {
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": True,
            "evidence_type": "DIRECT_VISUAL",
            "status": "POSITIVE",
            "conclusion": "裁剪放大后可见深灰色/黑色沙发。",
            "confidence": 0.94,
            "target_evidence": [
                {
                    "subject": "沙发",
                    "target": "黑色沙发",
                    "attributes": {"对象类别": "沙发", "颜色": "深灰色/黑色"},
                    "constraint_results": [
                        {"constraint": "对象类别", "expected": "沙发", "observed": "沙发", "status": "MATCH"},
                        {"constraint": "颜色", "expected": "黑色", "observed": "深灰色/黑色", "status": "MATCH"},
                    ],
                    "matches_query": True,
                    "location": "画面中央",
                    "bbox_1000": [100, 100, 900, 900],
                    "confidence": 0.94,
                }
            ],
            "absence_evidence": {},
        }

    reasoner._request_json = fake_request
    result = reasoner.analyze(
        "画面中是否有黑色的沙发",
        [{"camera_name": "展厅4", "snapshot_url": ONE_PIXEL}],
    )
    assert len(calls) == 3
    assert result["status"] == "UNCERTAIN"
    assert result["target_observed"] is None
    assert result["target_camera_names"] == []
    assert result["target_evidence"] == []
    assert result["confidence"] <= 0.5
    assert result["evidence_type"] == "INSUFFICIENT"
    assert result["candidate_model_outputs"][0]["initial_output"]["target_observed"] is True
    assert result["candidate_model_outputs"][0]["verification_output"]["target_observed"] is None
    assert result["candidate_model_outputs"][0]["attribute_audit"]["candidates"][0]["dominant_color"] == "深灰色"

    query_spec = reasoner.visual_query_spec("画面中是否有黑色的沙发")
    exact_evidence = {
        "matches_query": True,
        "confidence": 0.9,
        "constraint_results": [
            {"constraint": "对象类别", "expected": "沙发", "observed": "双人沙发", "status": "MATCH"},
            {"constraint": "颜色", "expected": "黑色", "observed": "深黑色", "status": "MATCH"},
        ],
    }
    assert reasoner._evidence_satisfies_query_predicate(query_spec, exact_evidence) is True
    close_colour = {
        **exact_evidence,
        "constraint_results": [
            {"constraint": "对象类别", "expected": "沙发", "observed": "双人沙发", "status": "MATCH"},
            {"constraint": "颜色", "expected": "黑色", "observed": "深棕色（接近黑色）", "status": "MATCH"},
        ],
    }
    assert reasoner._evidence_satisfies_query_predicate(query_spec, close_colour) is False


def run_verified_multibatch_merge_regression():
    """Hits in a later 2nd batch must survive evidence and UI metadata merge."""
    reasoner = VisualReasoner(
        {"api_key": "test", "model": "smoke-vlm", "candidate_batch_size": 2}
    )
    positive_cameras = {"展厅1", "展厅4"}

    def camera_from_content(content):
        content_text = str(content)
        return next(
            (f"展厅{index}" for index in range(1, 6) if f"展厅{index}" in content_text),
            "",
        )

    def verified_positive(camera_name):
        return {
            "relevance": 1,
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": True,
            "evidence_type": "DIRECT_VISUAL",
            "status": "POSITIVE",
            "conclusion": f"{camera_name}中可见黑色沙发。",
            "confidence": 0.94,
            "target_evidence": [
                {
                    "subject": "沙发",
                    "target": "黑色沙发",
                    "attributes": {"对象类别": "沙发", "颜色": "黑色"},
                    "constraint_results": [
                        {"constraint": "对象类别", "expected": "沙发", "observed": "沙发", "status": "MATCH"},
                        {"constraint": "黑色的沙发", "expected": "黑色的沙发", "observed": "黑色皮革沙发", "status": "MATCH"},
                    ],
                    "matches_query": True,
                    "location": "画面左侧",
                    "bbox_1000": [100, 300, 450, 900],
                    "confidence": 0.94,
                }
            ],
            "absence_evidence": {},
            "observations": [],
            "exclusions": [],
        }

    def verified_negative(camera_name):
        return {
            "relevance": 1,
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": False,
            "evidence_type": "ABSENCE",
            "status": "NEGATIVE",
            "conclusion": f"{camera_name}未发现黑色沙发。",
            "confidence": 0.9,
            "target_evidence": [],
            "absence_evidence": {
                "coverage": "FULL",
                "inspected_subject_count": 1,
                "reason": "已核对全画面中的沙发及颜色",
            },
            "observations": [],
            "exclusions": [],
        }

    def fake_request(system, content, max_tokens=512):
        camera_name = camera_from_content(content)
        if "候选镜头分析器" in system or "视觉判断器" in system:
            # Positive first passes are still independently reviewed.
            return verified_positive(camera_name) if camera_name in positive_cameras else verified_negative(camera_name)
        if "视觉目标复核器" in system:
            return verified_positive(camera_name) if camera_name in positive_cameras else verified_negative(camera_name)
        if "视觉颜色盲审器" in system:
            return {
                "candidates": [
                    {
                        "candidate_index": 1,
                        "subject": "沙发",
                        "dominant_color": "黑色",
                        "usable": True,
                        "confidence": 0.95,
                        "reason": "彩色画面可辨",
                    }
                ]
            }
        return {
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": False,
            "evidence_type": "ABSENCE",
            "status": "NEGATIVE",
            "conclusion": "批次汇总文本不参与确定性证据合并。",
            "confidence": 0.9,
            "selected_camera_names": [],
            "target_evidence": [],
            "absence_evidence": {"coverage": "FULL", "inspected_subject_count": 2, "reason": "批次已完成"},
            "observations": [],
            "exclusions": [],
        }

    reasoner._request_json = fake_request
    result = reasoner.analyze(
        "画面中是否有黑色的沙发",
        [{"camera_name": f"展厅{index}", "snapshot_url": ONE_PIXEL} for index in range(1, 6)],
    )
    assert result["batch_count"] == 3
    assert result["target_camera_names"] == ["展厅1", "展厅4"]
    assert [item["camera_name"] for item in result["target_evidence"]] == ["展厅1", "展厅4"]
    assert len(result["candidate_model_outputs"]) == 5
    assert result["selected_camera_names"] == ["展厅1", "展厅4"]


def run_conflicting_color_blind_audit_regression():
    """A query-blind colour tie-breaker must reject a dark-blue false hit."""
    reasoner = VisualReasoner({"api_key": "test", "model": "smoke-vlm"})
    calls = []
    audit_calls = []

    def visual_result(observed, positive):
        return {
            "business_policy": "OBSERVATION_ONLY",
            "target_observed": positive,
            "evidence_type": "DIRECT_VISUAL" if positive else "ABSENCE",
            "status": "POSITIVE" if positive else "NEGATIVE",
            "conclusion": "画面中存在黑色沙发。" if positive else "所有沙发均非黑色。",
            "confidence": 0.98,
            "target_evidence": ([{
                "subject": "沙发",
                "target": "黑色沙发",
                "attributes": {"color": observed},
                "constraint_results": [
                    {"constraint": "对象类别", "expected": "沙发", "observed": "单人沙发", "status": "MATCH"},
                    {"constraint": "黑色的沙发", "expected": "黑色的沙发", "observed": observed, "status": "MATCH"},
                ],
                "matches_query": True,
                "location": "画面右侧",
                "bbox_1000": [700, 200, 900, 500],
                "confidence": 0.98,
            }] if positive else []),
            "absence_evidence": (
                {} if positive else {"coverage": "FULL", "inspected_subject_count": 4, "reason": "已逐一核对全部沙发颜色"}
            ),
        }

    def fake_request(system, _content, max_tokens=512):
        calls.append(system)
        if "视觉判断器" in system:
            return visual_result("", False)
        if "视觉目标复核器" in system:
            return visual_result("黑色", True)
        assert "视觉颜色盲审器" in system
        assert "黑色" not in system
        audit_calls.append(1)
        if len(audit_calls) == 1:
            return {"candidates": []}
        return {
            "candidates": [
                {"candidate_index": 1, "subject": "单人休闲椅", "dominant_color": "深蓝色", "usable": True, "confidence": 0.94, "reason": "彩色画面可辨"}
            ]
        }

    reasoner._request_json = fake_request
    result = reasoner.analyze(
        "画面中是否有黑色的沙发",
        [{"camera_name": "展厅8", "snapshot_url": ONE_PIXEL}],
    )
    assert len(calls) == 4
    assert len(audit_calls) == 2
    assert result["status"] == "NEGATIVE"
    assert result["target_observed"] is False
    assert result["target_camera_names"] == []
    assert result["target_evidence"] == []
    output = result["candidate_model_outputs"][0]
    assert output["attribute_audit"]["candidates"][0]["dominant_color"] == "深蓝色"
    assert output["verification_output"]["target_evidence"][0]["constraint_results"][1]["status"] == "MISMATCH"


def run_negative_verifier_blind_audit_recovery_regression():
    """A blind crop audit may recover a first-pass hit rejected by an over-strict verifier."""
    reasoner = VisualReasoner({"api_key": "test", "model": "smoke-vlm"})
    calls = []

    def fake_request(system, _content, max_tokens=512):
        calls.append(system)
        if "视觉判断器" in system:
            return {
                "business_policy": "OBSERVATION_ONLY",
                "target_observed": True,
                "evidence_type": "DIRECT_VISUAL",
                "status": "POSITIVE",
                "conclusion": "画面中存在黑色沙发。",
                "confidence": 0.96,
                "target_evidence": [{
                    "subject": "沙发",
                    "target": "黑色沙发",
                    "attributes": {},
                    "constraint_results": [
                        {"constraint": "黑色", "expected": "黑色", "observed": "黑色", "status": "MATCH"},
                        {"constraint": "沙发", "expected": "沙发", "observed": "沙发", "status": "MATCH"},
                    ],
                    "matches_query": True,
                    "location": "画面左下角",
                    "bbox_1000": [100, 700, 400, 990],
                    "confidence": 0.96,
                }],
                "absence_evidence": {},
            }
        if "视觉目标复核器" in system:
            return {
                "business_policy": "OBSERVATION_ONLY",
                "target_observed": False,
                "evidence_type": "ABSENCE",
                "status": "NEGATIVE",
                "conclusion": "深色皮革沙发无法确认为黑色。",
                "confidence": 0.9,
                "target_evidence": [],
                "absence_evidence": {"coverage": "FULL", "inspected_subject_count": 3, "reason": "逐一检查全部沙发"},
            }
        assert "视觉颜色盲审器" in system
        assert "黑色" not in system
        return {
            "candidates": [
                {"candidate_index": 1, "subject": "浅色沙发", "dominant_color": "灰色", "usable": True, "confidence": 0.95, "reason": "彩色画面可辨"},
                {"candidate_index": 2, "subject": "皮革沙发", "dominant_color": "黑色", "usable": True, "confidence": 0.94, "reason": "彩色画面可辨"},
                {"candidate_index": 3, "subject": "单人椅", "dominant_color": "棕色", "usable": True, "confidence": 0.96, "reason": "彩色画面可辨"},
                {"candidate_index": 4, "subject": "皮革沙发", "dominant_color": "黑色", "usable": True, "confidence": 0.93, "reason": "彩色画面可辨"},
            ]
        }

    reasoner._request_json = fake_request
    result = reasoner.analyze(
        "画面中是否有黑色的沙发",
        [{
            "camera_name": "展厅10",
            "snapshot_url": ONE_PIXEL,
            "_ovd_candidates": {
                "state": "READY",
                "detections": [
                    {"prompt": "sofa", "bbox_1000": [400, 200, 800, 500], "score": 0.91},
                    {"prompt": "sofa", "bbox_1000": [80, 350, 360, 750], "score": 0.88},
                    {"prompt": "sofa", "bbox_1000": [100, 800, 390, 1000], "score": 0.86},
                    {"prompt": "sofa", "bbox_1000": [100, 370, 340, 730], "score": 0.84},
                ],
            },
        }],
    )
    assert len(calls) == 3
    assert result["status"] == "POSITIVE"
    assert result["target_observed"] is True
    assert result["selected_camera_names"] == ["展厅10"], result
    assert result["candidate_model_outputs"][0]["verification_output"]["target_observed"] is True
    assert result["candidate_model_outputs"][0]["preaudit_verification_output"]["target_observed"] is False
    assert result["target_evidence"][0]["bbox_1000"] == [80, 350, 360, 750]
    assert len(result["target_evidence"]) == 1
    assert result["candidate_model_outputs"][0]["attribute_audit"]["candidates"][1]["dominant_color"] == "黑色"

    many_evidence = [
        {
            "subject": "沙发",
            "target": "黑色沙发",
            "matches_query": True,
            "location": f"镜头{index}",
            "bbox_1000": [10, 10, 100, 100],
            "confidence": 0.9,
        }
        for index in range(17)
    ]
    assert len(reasoner._normalize_target_evidence(many_evidence)) == 17


def main():
    spec = VisualReasoner.visual_query_spec("帮我看下是否有背复古邮差包的顾客")
    assert spec["query_mode"] == "EXISTENCE"
    assert spec["requires_human_enumeration"] is True
    assert VisualReasoner.visual_query_spec("画面里有穿红衣服的人吗？")["query_mode"] == "EXISTENCE"
    run_red_clothing_regression()
    run_open_vocabulary_backpack_regression()
    run_weak_absence_becomes_uncertain_regression()
    run_complete_absence_regression()
    run_attribute_false_positive_review_regression()
    run_verified_multibatch_merge_regression()
    run_conflicting_color_blind_audit_regression()
    run_negative_verifier_blind_audit_recovery_regression()
    print("PASS visual dynamic query tests: localized predicates, positive review, full batch merge, evidence-gated absence")


if __name__ == "__main__":
    main()
