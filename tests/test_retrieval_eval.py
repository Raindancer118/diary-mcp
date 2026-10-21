"""Tunable prompt retrieval + offline evaluation (v0.27.0).

Motivation: the injection log showed 12 injections in 3628 prompts (0.3 %).
Whether that is "correctly cautious" or "dead" can't be told without a
labelled prompt set — so thresholds become parameters and an eval harness
measures precision/recall for any parameter set.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _upsert(path, title="Title", body="Body", importance=0.5, embedding=None):
    import diary_server
    with patch("diary_embed.embed", return_value=embedding), \
         patch("diary_embed.embed_many", side_effect=lambda ts: [embedding] * len(ts)):
        return diary_server.memory_upsert(path=path, title=title, body=body, importance=importance)


def _conn():
    import diary_db
    return diary_db.get_db()


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DIARY_HOOK_STATE_DIR", str(tmp_path / "state"))


def _seed():
    _upsert("/projects/demo/rsync-regel", "Deployment per rsync",
            "Beim rsync auf Dorn immer --exclude .env setzen, sonst Produktionssecrets weg.", 0.9)
    for i in range(12):
        _upsert(f"/projects/demo/filler-{i}", f"Projekt Notiz {i}",
                f"Allgemeine Projekt Notiz über das Projekt Nummer {i}.", 0.5)


PARTIAL = "rsync Deployment auf Dorn und danach Kubernetes Helm Ingress Chart konfigurieren"


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def test_default_params_mirror_module_constants():
    import memory_injection as mi
    p = mi.RetrievalParams()
    assert (p.max_hits, p.min_confidence, p.min_importance) == \
        (mi.PROMPT_MAX_HITS, mi.MIN_CONFIDENCE, mi.MIN_IMPORTANCE)
    assert (p.sem_sim_zero, p.sem_sim_full) == (mi.SEM_SIM_ZERO, mi.SEM_SIM_FULL)


def test_lower_confidence_param_admits_partial_match():
    import memory_injection as mi
    _seed()
    with _conn() as conn:
        assert mi.retrieve_for_prompt(conn, PARTIAL, "demo") == []
        hits = mi.retrieve_for_prompt(conn, PARTIAL, "demo",
                                      params=mi.RetrievalParams(mode="threshold", min_confidence=0.2))
    assert [h["path"] for h in hits] == ["/projects/demo/rsync-regel"]


def test_candidates_keep_raw_signals_and_selection_matches_retrieve():
    import memory_injection as mi
    vec = [1.0] + [0.0] * 383
    _seed()
    _upsert("/projects/demo/sem", "Fahrzeugpflege", "Winterreifen im Oktober wechseln.", 0.7, embedding=vec)
    with _conn() as conn:
        cands = mi.score_candidates(conn, PARTIAL, "demo", qvec=vec)
        direct = mi.retrieve_for_prompt(conn, PARTIAL, "demo", qvec=vec)
    # selection over precomputed candidates must equal the one-shot call (live mode)
    by_path = {c["path"]: c for c in cands}
    assert by_path["/projects/demo/rsync-regel"]["lex_conf"] > 0
    assert by_path["/projects/demo/sem"]["sim"] == pytest.approx(1.0, abs=1e-4)
    assert [h["path"] for h in mi.select_hits(cands, mi.RetrievalParams())] == [h["path"] for h in direct]


def test_semantic_calibration_is_a_parameter():
    import memory_injection as mi
    vec = [1.0] + [0.0] * 383
    far = [0.6, 0.8] + [0.0] * 382  # cosine 0.6 → below default calibration
    _upsert("/projects/demo/sem", "Fahrzeugpflege", "Winterreifen im Oktober wechseln.", 0.7, embedding=vec)
    with _conn() as conn:
        cands = mi.score_candidates(conn, "Pneus tauschen?", "demo", qvec=far)
    assert mi.select_hits(cands, mi.RetrievalParams(mode="threshold")) == []
    loose = mi.RetrievalParams(mode="threshold", sem_sim_zero=0.3, sem_sim_full=0.6)
    assert [h["path"] for h in mi.select_hits(cands, loose)] == ["/projects/demo/sem"]


# ---------------------------------------------------------------------------
# Near-miss logging: why nothing was injected
# ---------------------------------------------------------------------------

def test_prompt_log_records_best_near_miss_without_content(tmp_path):
    import memory_injection as mi
    _seed()
    with patch("memory_injection._query_vector", return_value=(None, "no_socket")):
        assert mi.run_hook("prompt", {"session_id": "n1", "cwd": "/x/demo", "prompt": PARTIAL}) == ""
    rec = json.loads((tmp_path / "state" / "injection_log.jsonl").read_text().splitlines()[-1])
    assert rec["hits"] == [] and rec["semantic"] is False
    assert rec["semantic_reason"] == "no_socket"
    assert rec["reason"] == "below_confidence"
    assert rec["best"]["path"] == "/projects/demo/rsync-regel"
    assert 0 < rec["best"]["conf"] < mi.MIN_CONFIDENCE
    assert "rsync" not in json.dumps(rec["best"]).replace("rsync-regel", "")


def test_prompt_log_reason_for_trivial_prompt(tmp_path):
    import memory_injection as mi
    _seed()
    with patch("memory_injection._query_vector", return_value=(None, "no_socket")):
        mi.run_hook("prompt", {"session_id": "n2", "cwd": "/x/demo", "prompt": "ja passt"})
    rec = json.loads((tmp_path / "state" / "injection_log.jsonl").read_text().splitlines()[-1])
    assert rec["reason"] == "no_candidates"


def test_query_vector_reports_reason_when_socket_missing(monkeypatch, tmp_path):
    import diary_embed_ipc
    import memory_injection as mi
    monkeypatch.setattr(diary_embed_ipc, "sock_path", lambda: tmp_path / "missing.sock")
    assert mi._query_vector("hallo welt") == (None, "no_socket")


# ---------------------------------------------------------------------------
# Eval harness
# ---------------------------------------------------------------------------

def _gold_file(tmp_path, rows):
    f = tmp_path / "gold.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return f


def test_load_gold_validates_rows(tmp_path):
    import retrieval_eval as ev
    f = _gold_file(tmp_path, [
        {"prompt": "rsync auf Dorn", "slug": "demo", "expected": ["/projects/demo/rsync-regel"]},
        {"prompt": "ja mach weiter", "expected": []},
    ])
    gold = ev.load_gold(f)
    assert len(gold) == 2 and gold[1].expected == () and gold[1].acceptable == ()
    bad = _gold_file(tmp_path, [{"expected": []}])
    with pytest.raises(ValueError, match="line 1"):
        ev.load_gold(bad)


def test_evaluate_counts_hits_misses_and_false_positives(tmp_path):
    import memory_injection as mi
    import retrieval_eval as ev
    _seed()
    gold = ev.load_gold(_gold_file(tmp_path, [
        {"prompt": "Deployment auf Dorn per rsync bitte", "slug": "demo",
         "expected": ["/projects/demo/rsync-regel"]},
        {"prompt": PARTIAL, "slug": "demo", "expected": ["/projects/demo/rsync-regel"]},
        {"prompt": "Produktionssecrets auf Dorn per rsync exclude", "slug": "demo", "expected": []},
    ]))
    with _conn() as conn:
        cands = ev.collect_candidates(conn, gold, embed=lambda _p: None)
    strict = ev.evaluate(gold, cands, mi.RetrievalParams(mode="threshold"))
    assert strict.positives == 2 and strict.negatives == 1
    assert strict.recall == pytest.approx(0.5)
    loose = ev.evaluate(gold, cands, mi.RetrievalParams(mode="threshold", min_confidence=0.0,
                                                        min_importance=0.0, max_hits=3))
    assert loose.recall == 1.0
    assert loose.negative_fp_rate == 1.0  # the rsync rule looks relevant but is labelled wrong here
    assert loose.precision < 1.0


def test_acceptable_paths_are_not_false_positives(tmp_path):
    import memory_injection as mi
    import retrieval_eval as ev
    _seed()
    gold = ev.load_gold(_gold_file(tmp_path, [
        {"prompt": "Deployment auf Dorn per rsync bitte", "slug": "demo",
         "expected": [], "acceptable": ["/projects/demo/rsync-regel"]},
    ]))
    with _conn() as conn:
        cands = ev.collect_candidates(conn, gold, embed=lambda _p: None)
    res = ev.evaluate(gold, cands, mi.RetrievalParams(mode="threshold"))
    assert res.injected == 1 and res.precision == 1.0 and res.negative_fp_rate == 0.0


def test_sweep_recommends_highest_recall_meeting_precision_target(tmp_path):
    import memory_injection as mi
    import retrieval_eval as ev
    _seed()
    gold = ev.load_gold(_gold_file(tmp_path, [
        {"prompt": "Deployment auf Dorn per rsync bitte", "slug": "demo",
         "expected": ["/projects/demo/rsync-regel"]},
        {"prompt": PARTIAL, "slug": "demo", "expected": ["/projects/demo/rsync-regel"]},
        {"prompt": "Produktionssecrets auf Dorn per rsync exclude", "slug": "demo", "expected": []},
    ]))
    with _conn() as conn:
        cands = ev.collect_candidates(conn, gold, embed=lambda _p: None)
    rows = ev.sweep(gold, cands, grid={"min_confidence": [0.9, 0.5, 0.0], "min_importance": [0.5, 0.0]},
                    base=mi.RetrievalParams(mode="threshold"))
    best = ev.recommend(rows, precision_target=0.9)
    assert best is not None and best.result.precision >= 0.9
    assert best.result.recall == max(r.result.recall for r in rows if r.result.precision >= 0.9)


def test_report_is_readable_text(tmp_path):
    import memory_injection as mi
    import retrieval_eval as ev
    _seed()
    gold = ev.load_gold(_gold_file(tmp_path, [
        {"prompt": PARTIAL, "slug": "demo", "expected": ["/projects/demo/rsync-regel"]},
    ]))
    with _conn() as conn:
        cands = ev.collect_candidates(conn, gold, embed=lambda _p: None)
    text = ev.format_failures(gold, cands, mi.RetrievalParams(mode="threshold"))
    assert "MISS" in text and "/projects/demo/rsync-regel" in text


def test_eval_entry_point_declared():
    import tomllib
    from pathlib import Path
    data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["diary-eval"] == "retrieval_eval:main"


# ---------------------------------------------------------------------------
# Learned gate (v0.28.0): a logistic model over relative features beats any
# absolute threshold — positives and negatives overlap in raw similarity, but
# the right memory is usually the clear #1 of both signals.
# ---------------------------------------------------------------------------

def _cands():
    return [
        {"path": "/projects/demo/a", "title": "A", "body": "", "importance": 0.8,
         "lex_conf": 0.6, "sim": 0.70, "in_project": True},
        {"path": "/projects/other/b", "title": "B", "body": "", "importance": 0.8,
         "lex_conf": 0.2, "sim": 0.50, "in_project": False},
        {"path": "/projects/other/c", "title": "C", "body": "", "importance": 0.8,
         "lex_conf": 0.0, "sim": 0.45, "in_project": False},
    ]


def test_features_capture_rank_margin_agreement_and_project():
    import memory_injection as mi
    feats = dict(zip(mi.FEATURE_NAMES, mi.candidate_features(_cands())[0]))
    assert feats["sem_top"] == 1.0 and feats["lex_top"] == 1.0 and feats["both_top"] == 1.0
    assert feats["sem_margin"] == pytest.approx(0.20)
    assert feats["lex_margin"] == pytest.approx(0.40)
    assert feats["in_project"] == 1.0
    second = dict(zip(mi.FEATURE_NAMES, mi.candidate_features(_cands())[1]))
    assert second["sem_top"] == 0.0 and second["sem_margin"] == pytest.approx(-0.20)


def test_learned_mode_ranks_by_model_probability():
    import memory_injection as mi
    params = mi.RetrievalParams(mode="learned", min_prob=0.0, max_hits=3)
    ranked = mi.select_hits(_cands(), params)
    assert [h["path"] for h in ranked] == ["/projects/demo/a", "/projects/other/b", "/projects/other/c"]
    assert ranked[0]["confidence"] > ranked[1]["confidence"]
    assert all(0.0 <= h["confidence"] <= 1.0 for h in ranked)


def test_learned_mode_gate_and_importance_floor():
    import memory_injection as mi
    model = mi.InjectionModel(weights={n: 0.0 for n in mi.FEATURE_NAMES} | {"bias": 5.0})
    params = mi.RetrievalParams(mode="learned", min_prob=0.9, max_hits=2)
    cands = _cands()
    cands[0]["importance"] = 0.2
    hits = mi.select_hits(cands, params, model=model)
    assert [h["path"] for h in hits] == ["/projects/other/b", "/projects/other/c"]
    strict = mi.InjectionModel(weights={n: 0.0 for n in mi.FEATURE_NAMES} | {"bias": -5.0})
    assert mi.select_hits(_cands(), params, model=strict) == []


def test_model_file_overrides_default_and_bad_file_falls_back(tmp_path, monkeypatch):
    import memory_injection as mi
    monkeypatch.setenv("DIARY_INJECTION_MODEL", str(tmp_path / "m.json"))
    assert mi.load_model() == mi.DEFAULT_MODEL
    custom = mi.InjectionModel(weights={n: 0.5 for n in mi.FEATURE_NAMES} | {"bias": -1.0},
                               min_prob=0.7, trained_on=12)
    mi.save_model(custom)
    assert mi.load_model() == custom
    (tmp_path / "m.json").write_text('{"weights": {"unknown_feature": 1}}')
    assert mi.load_model() == mi.DEFAULT_MODEL


def test_default_params_use_learned_mode_and_model_threshold():
    import memory_injection as mi
    assert mi.DEFAULT_PARAMS.mode == "learned"
    assert mi.DEFAULT_PARAMS.min_prob is None  # → the loaded model's own threshold


def test_in_project_flag_set_by_score_candidates():
    import memory_injection as mi
    _seed()
    _upsert("/projects/other/rsync", "Deployment per rsync", "rsync auf Dorn mit exclude .env.", 0.9)
    with _conn() as conn:
        cands = {c["path"]: c for c in mi.score_candidates(conn, "Deployment auf Dorn per rsync", "demo")}
    assert cands["/projects/demo/rsync-regel"]["in_project"] is True
    assert cands["/projects/other/rsync"]["in_project"] is False


def test_fit_separates_and_cross_validation_never_sees_its_own_item(tmp_path):
    import memory_injection as mi
    import retrieval_eval as ev
    gold, cands = [], []
    for i in range(20):
        good = {"path": f"/p/good{i}", "title": "", "body": "", "importance": 0.8,
                "lex_conf": 0.7, "sim": 0.7, "in_project": True}
        bad = {"path": f"/p/bad{i}", "title": "", "body": "", "importance": 0.8,
               "lex_conf": 0.1, "sim": 0.4, "in_project": False}
        gold.append(ev.GoldItem(f"prompt {i}", "p", (good["path"],), ()))
        cands.append([good, bad])
    model = ev.fit_model(gold, cands)
    probs = {h["path"]: h["confidence"] for h in
             mi.select_hits(cands[0], mi.RetrievalParams(mode="learned", min_prob=0.0, max_hits=2), model=model)}
    assert probs["/p/good0"] > 0.8 > 0.2 > probs["/p/bad0"]
    preds = ev.cross_validate(gold, cands, folds=5)
    assert len(preds) == 20 and all(len(p) == 2 for p in preds)
    res = ev.evaluate_predictions(gold, preds, min_prob=0.5, max_hits=1)
    assert res.recall == 1.0 and res.precision == 1.0
