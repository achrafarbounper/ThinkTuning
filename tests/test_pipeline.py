# project/tests/test_pipeline.py
"""Tests du pipeline end-to-end (SCRUM-39) : pipeline.py (CLI), le runner
(core/pipeline_runner.py) et l'API /pipeline.

Aucun labeling ni fine-tuning réel : label_dataset et le subprocess
finetune_llm.py sont remplacés par des doubles.
"""

import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.models import PipelineRequest, TrainJob, JobStatus
from core import pipeline_runner
import pipeline as pipeline_cli


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture()
def fake_label(monkeypatch):
    """Remplace run_labeling : renvoie N records sans toucher au modèle."""
    calls = {}

    def _fake(params, labeled_output):
        calls["params"] = params
        calls["labeled_output"] = labeled_output
        import json
        from pathlib import Path
        out = Path(labeled_output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            for i in range(3):
                handle.write(json.dumps({
                    "instruction": "x", "input": f"texte {i}",
                    "output": "positive", "confidence": 0.9,
                }) + "\n")
        return [{"instruction": "x"}] * 3

    monkeypatch.setattr(pipeline_runner, "run_labeling", _fake)
    calls["fn"] = _fake
    return calls


@pytest.fixture()
def fake_popen(monkeypatch):
    """Subprocess Popen factice : se termine immédiatement avec le code 0."""
    calls = {}

    class FakeProcess:
        def __init__(self, cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            self.returncode = 0
            self.stdout = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(pipeline_runner.subprocess, "Popen", FakeProcess)
    calls["cls"] = FakeProcess
    return calls


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """Client FastAPI avec job store isolé (tmp_path) et runner mocké."""
    monkeypatch.setenv("API_KEY", "test-key")

    from core.job_store import PersistentJobStore
    from api.routes import pipeline as pipeline_route

    store = PersistentJobStore(path=str(tmp_path / "jobs.db"))
    monkeypatch.setattr(pipeline_route, "get_job_store", lambda: store)
    monkeypatch.setattr(pipeline_route, "run_pipeline", lambda job_id, req: None)

    from api.main import app

    yield TestClient(app), store


# --------------------------------------------------------------------------- #
# build_finetune_cmd
# --------------------------------------------------------------------------- #

def test_build_finetune_cmd_defaults():
    req = PipelineRequest(input_path="data.csv")
    cmd = pipeline_runner.build_finetune_cmd(req, "labeled.jsonl", "out_dir")
    assert cmd[1].endswith("finetune_llm.py")
    assert cmd[cmd.index("--train_file") + 1] == "labeled.jsonl"
    assert cmd[cmd.index("--output_dir") + 1] == "out_dir"
    assert "--use_qlora" in cmd
    # Paramètres None -> absents de la commande (défauts de finetune_llm.py).
    assert "--epochs" not in cmd
    assert "--base_model" not in cmd


def test_build_finetune_cmd_overrides_and_no_qlora():
    req = PipelineRequest(
        input_path="data.csv",
        base_model="TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        epochs=2,
        learning_rate=1e-4,
        lora_r=8,
        use_qlora=False,
    )
    cmd = pipeline_runner.build_finetune_cmd(req, "t.jsonl", "o")
    assert cmd[cmd.index("--epochs") + 1] == "2"
    assert cmd[cmd.index("--learning_rate") + 1] == "0.0001"
    assert cmd[cmd.index("--lora_r") + 1] == "8"
    assert "--no_qlora" in cmd


# --------------------------------------------------------------------------- #
# run_pipeline (runner persisté, thread target)
# --------------------------------------------------------------------------- #

def test_run_pipeline_completes(tmp_path, fake_label, fake_popen):
    from core.job_store import PersistentJobStore

    store = PersistentJobStore(path=str(tmp_path / "jobs.db"))
    monkeypatch_store = patch.object(pipeline_runner, "get_job_store", lambda: store)
    with monkeypatch_store:
        job = TrainJob(job_id="job-1", status=JobStatus.PENDING)
        store["job-1"] = job

        req = PipelineRequest(input_path="data/unlabeled.csv", min_confidence=0.8, epochs=1)
        pipeline_runner.run_pipeline("job-1", req)

        result = store["job-1"]
        assert result.status == JobStatus.COMPLETED
        assert result.step == "done"
        assert result.model_path.endswith("lora_model")
        assert fake_label["params"].min_confidence == 0.8
        # Garde-fou passé : le subprocess finetune a bien été construit.
        assert fake_popen["cmd"][1].endswith("finetune_llm.py")
        assert fake_popen["cmd"][fake_popen["cmd"].index("--train_file") + 1] == \
            fake_label["labeled_output"]


def test_run_pipeline_guard_empty_dataset(tmp_path, monkeypatch, fake_popen):
    """0 record au-dessus du seuil : job FAILED, finetune jamais lancé."""
    from core.job_store import PersistentJobStore

    monkeypatch.setattr(pipeline_runner, "run_labeling", lambda params, out: [])
    store = PersistentJobStore(path=str(tmp_path / "jobs.db"))
    with patch.object(pipeline_runner, "get_job_store", lambda: store):
        store["job-2"] = TrainJob(job_id="job-2", status=JobStatus.PENDING)
        req = PipelineRequest(input_path="data.csv")
        pipeline_runner.run_pipeline("job-2", req)

        result = store["job-2"]
        assert result.status == JobStatus.FAILED
        assert "aucun record" in result.error.lower()
        assert "min_confidence" in result.error


def test_run_pipeline_cancel(tmp_path, fake_label):
    """Annulation entre le labeling et le fine-tuning : subprocess non lancé."""
    from core.job_store import PersistentJobStore

    store = PersistentJobStore(path=str(tmp_path / "jobs.db"))

    def fake_finetune(cmd, cancel_event=None, cwd=None):
        raise RuntimeError("Pipeline cancelled by user")

    with patch.object(pipeline_runner, "get_job_store", lambda: store), \
         patch.object(pipeline_runner, "run_finetune", fake_finetune):
        store["job-3"] = TrainJob(job_id="job-3", status=JobStatus.PENDING)
        pipeline_runner.get_cancel_event("job-3").set()  # annulé avant démarrage
        req = PipelineRequest(input_path="data.csv")
        pipeline_runner.run_pipeline("job-3", req)

        result = store["job-3"]
        assert result.status == JobStatus.CANCELLED
        assert result.step == "cancelled"


def test_run_finetune_failure_raises(monkeypatch):
    class FailingProcess:
        def __init__(self, cmd, **kwargs):
            self.returncode = 1
            self.stdout = None

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(pipeline_runner.subprocess, "Popen", FailingProcess)
    with pytest.raises(RuntimeError, match="finetune_llm.py a échoué"):
        pipeline_runner.run_finetune(["python", "finetune_llm.py"])


# --------------------------------------------------------------------------- #
# pipeline.py — CLI, YAML, fusion CLI > YAML
# --------------------------------------------------------------------------- #

def test_merge_params_yaml_and_cli_precedence(tmp_path):
    config = tmp_path / "pipeline.yaml"
    config.write_text(
        "labeling:\n"
        "  min_confidence: 0.9\n"
        "  text_column: content\n"
        "finetune:\n"
        "  epochs: 5\n"
        "  use_qlora: false\n",
        encoding="utf-8",
    )
    args = pipeline_cli.build_parser().parse_args([
        "--input", "data.csv",
        "--output_dir", "out",
        "--config", str(config),
        "--min_confidence", "0.5",   # CLI doit gagner sur YAML (0.9)
    ])
    params = pipeline_cli.merge_params(args)
    assert params.min_confidence == 0.5          # CLI > YAML
    assert params.text_column == "content"        # YAML > défaut
    assert params.epochs == 5                     # YAML > défaut
    assert params.use_qlora is False              # YAML > défaut
    assert params.label_batch_size == 32          # défaut


def test_merge_params_rejects_unknown_keys(tmp_path):
    config = tmp_path / "pipeline.yaml"
    config.write_text("labeling:\n  cle_inconnue: 1\n", encoding="utf-8")
    args = pipeline_cli.build_parser().parse_args([
        "--input", "data.csv", "--output_dir", "out", "--config", str(config),
    ])
    with pytest.raises(SystemExit, match="cle_inconnue"):
        pipeline_cli.merge_params(args)


def test_cli_main_runs_labeling_then_finetune(tmp_path, monkeypatch, capsys):
    """La commande CLI enchaîne labeling puis subprocess finetune_llm.py."""
    labeled = tmp_path / "labeled.jsonl"
    calls = {}

    def fake_labeling(params, labeled_output):
        calls["params"] = params
        labeled.write_text("{}", encoding="utf-8")
        return [{"ok": True}]

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pipeline_cli, "run_labeling", fake_labeling)
    monkeypatch.setattr(pipeline_cli.subprocess, "run", fake_run)
    # Variadique : le handler rich (ia/logging_setup) appelle time.strftime(fmt,
    # t) pour afficher sa colonne temps ; la valeur retournée est identique.
    monkeypatch.setattr(pipeline_cli.time, "strftime", lambda *a: "20260101T000000Z")
    argv = [
        "pipeline.py", "--input", "data.csv", "--output_dir", str(tmp_path / "out"),
        "--epochs", "1",
    ]
    with patch.object(sys, "argv", argv):
        rc = pipeline_cli.main()
    assert rc == 0
    assert calls["cmd"][1].endswith("finetune_llm.py")
    assert calls["cmd"][calls["cmd"].index("--epochs") + 1] == "1"


def test_cli_main_empty_dataset_aborts(tmp_path, monkeypatch):
    """Garde-fou CLI : aucun record -> exit 1, finetune non lancé."""
    monkeypatch.setattr(pipeline_cli, "run_labeling", lambda params, out: [])
    launched = []
    monkeypatch.setattr(
        pipeline_cli.subprocess, "run",
        lambda cmd, **kw: launched.append(cmd) or SimpleNamespace(returncode=0),
    )
    argv = ["pipeline.py", "--input", "data.csv", "--output_dir", str(tmp_path / "out")]
    with patch.object(sys, "argv", argv):
        rc = pipeline_cli.main()
    assert rc == 1
    assert not launched


def test_cli_main_finetune_failure_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline_cli, "run_labeling", lambda params, out: [{"ok": True}]
    )
    monkeypatch.setattr(
        pipeline_cli.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=3),
    )
    argv = ["pipeline.py", "--input", "data.csv", "--output_dir", str(tmp_path / "out")]
    with patch.object(sys, "argv", argv):
        rc = pipeline_cli.main()
    assert rc == 3


# --------------------------------------------------------------------------- #
# API /pipeline
# --------------------------------------------------------------------------- #

HEADERS = {"X-API-Key": "test-key"}


def test_api_start_pipeline_and_status(api_client):
    client, store = api_client
    resp = client.post(
        "/pipeline",
        json={"input_path": "data/unlabeled.csv", "min_confidence": 0.9},
        headers=HEADERS,
    )
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["status"] == "pending"
    assert job["step"] == "queued"

    # run_pipeline est mocké : le job reste en pending, le status répond.
    status = client.get(f"/pipeline/status/{job['job_id']}", headers=HEADERS)
    assert status.status_code == 200
    assert status.json()["job_id"] == job["job_id"]


def test_api_status_unknown_job(api_client):
    client, _ = api_client
    resp = client.get("/pipeline/status/inconnu", headers=HEADERS)
    assert resp.status_code == 404


def test_api_pipeline_requires_api_key(api_client):
    client, _ = api_client
    assert client.post("/pipeline", json={"input_path": "x"}).status_code == 401
    assert client.get("/pipeline/jobs").status_code == 401


def test_api_cancel_unknown_job(api_client):
    client, _ = api_client
    assert client.post("/pipeline/cancel/inconnu", headers=HEADERS).status_code == 404


def test_api_list_pipeline_jobs(api_client):
    client, store = api_client
    store["job-a"] = TrainJob(
        job_id="job-a", status=JobStatus.COMPLETED, step="done", started_at=time.time()
    )
    store["job-b"] = TrainJob(
        job_id="job-b", status=JobStatus.RUNNING, step="finetuning", started_at=time.time()
    )

    resp = client.get("/pipeline/jobs", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert {item["job_id"] for item in data["items"]} == {"job-a", "job-b"}

    resp = client.get("/pipeline/jobs?status=completed", headers=HEADERS)
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["job_id"] == "job-a"
