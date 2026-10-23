import copy
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from battery_insight import copilot_cli as cli
from battery_insight import traceability as trace
from battery_insight.library import identity
from battery_insight.traceability_demo import create_demo
from battery_insight.unified_entry import main


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    project = tmp_path_factory.mktemp("copilot_base") / "project"
    create_demo(project, reviewed=False)
    return project


@pytest.fixture
def project(tmp_path, base):
    return shutil.copytree(base, tmp_path / "project")


def job_for(project):
    snap = trace.knowledge(project)
    paths = cli.prepare(project, [snap["documents"][0]["document_id"]])["requests"]
    path = next(Path(path) for path in paths if any(
        row["text"] == "합성 배합" for row in json.loads(Path(path).read_text(encoding="utf-8"))["evidence"]))
    return path, json.loads(path.read_text(encoding="utf-8"))


def response_for(job):
    def ref(text):
        return next(row["evidence_id"] for row in job["evidence"] if row["text"] == text)
    return {"items": [{"fields": [
        {"header": "항목명", "value": "합성 배합", "evidence_ids": [ref("합성 배합")]},
        {"header": "공정종류", "value": "배합", "evidence_ids": [ref("배합")]},
        {"header": "공정ID", "value": "SYN-MIX-001", "evidence_ids": [ref("SYN-MIX-001")]},
    ]}]}


def test_prepare_is_offline_stable_and_contains_no_raw_curves(project, monkeypatch):
    monkeypatch.setattr(cli, "_call", lambda *a, **kw: pytest.fail("LLM must not run"))
    path, job = job_for(project)
    again = cli.prepare(project, [job["document_id"]])
    assert str(path) in again["requests"] and again["llm_calls"] == 0
    text = cli.prompt_for(job)
    assert "step_time_s" not in text and ".parquet" not in text
    assert not trace.snapshot(project)["entities"]


def test_run_cache_and_portable_import_create_only_review_candidates(project, monkeypatch, tmp_path):
    path, job = job_for(project)
    response = response_for(job)
    calls = []
    def fake(settings, args, *, prompt=None):
        calls.append((args, prompt))
        if "--help" in args:
            return "copilot " + " ".join(cli.FLAGS)
        if "--version" in args:
            return "Copilot CLI fake-contract-test"
        assert "--available-tools=" in args and "--trust-folder" in args
        assert not any("allow-all" in arg for arg in args)
        assert json.loads(prompt.split("장표 원문 JSON:\n")[1]) == job
        return json.dumps(response, ensure_ascii=False)
    monkeypatch.setattr(cli, "_call", fake)
    result = cli.run_request(path, cli.checked_config({}))
    assert result["llm_calls"] == 1 and len(calls) == 3
    assert cli.run_request(path, cli.checked_config({}))["llm_calls"] == 0
    assert len(calls) == 3
    moved = shutil.copytree(project, tmp_path / "other_pc")
    before = len(trace.snapshot(moved)["proposals"])
    saved = cli.import_result(moved, result["output"])
    assert saved["added"] == 1
    assert cli.import_result(moved, result["output"])["added"] == 0
    after = trace.snapshot(moved)
    assert len(after["proposals"]) == before + 1
    assert not after["entities"] and not after["records"] and not after["raw_links"]
    candidate = next(row for row in after["proposals"] if row.get("extraction"))
    assert candidate["state"] == "pending" and candidate["payload"]["status"] == "draft"


@pytest.mark.parametrize("change", ["unknown_ref", "invented_value", "extra_operation", "bad_header"])
def test_bad_model_output_is_rejected_atomically(project, change):
    _, job = job_for(project)
    value = response_for(job)
    field = value["items"][0]["fields"][0]
    if change == "unknown_ref":
        field["evidence_ids"] = ["not-provided"]
    elif change == "invented_value":
        field["value"] = "invented-cell-999"
    elif change == "extra_operation":
        value["execute"] = "link all raw"
    else:
        field["header"] = "execute_command"
    with pytest.raises(ValueError):
        cli.proposals(job, value)
    assert not trace.snapshot(project)["entities"]


def test_tampered_request_cannot_import_against_current_slide_evidence(project, tmp_path):
    _, job = job_for(project)
    job = copy.deepcopy(job)
    job["evidence"][0]["text"] += " altered"
    job["job_id"] = identity({key: value for key, value in job.items() if key != "job_id"})
    response = {"schema_version": cli.RESULT_SCHEMA, "provider": "github_copilot_cli", "request": job, "response": response_for(job)}
    path = tmp_path / "response.json"
    path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="근거"):
        cli.import_result(project, path)


@pytest.mark.parametrize("settings", [{"model": "x & calc"}, {"timeout_s": 0}, {"api_key": "should-not-store"}])
def test_only_path_model_and_timeout_are_configurable(settings):
    with pytest.raises(ValueError):
        cli.checked_config(settings)


def test_old_cli_and_non_json_response_fail_without_fallback(monkeypatch):
    monkeypatch.setattr(cli, "_call", lambda *a, **kw: "Copilot old CLI")
    with pytest.raises(ValueError, match="옵션"):
        cli.check_cli(cli.checked_config({}))
    with pytest.raises(ValueError):
        cli._strict_json('{"items":[],"items":[]}')
    with pytest.raises(ValueError):
        cli._strict_json("I executed the commands.")


def test_real_windows_cmd_bridge_sends_unicode_and_metacharacters_only_on_stdin(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows CMD launcher")
    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import json,sys,pathlib,os\n"
        "text=sys.stdin.buffer.read().decode('utf-8')\n"
        "agents=list(pathlib.Path('.github/agents').glob('*.agent.md'))\n"
        "result={'received':text,'args':sys.argv[1:],'tools_disabled':len(agents)==1 and 'tools: []' in agents[0].read_text(encoding='utf-8'),'cwd':os.getcwd()}\n"
        "sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf-8'))\n", encoding="utf-8")
    batch = tmp_path / "fake copilot.cmd"
    batch.write_text('@"' + sys.executable + '" "' + str(script) + '" %*\n', encoding="utf-8")
    prompt = "장표: & | < > % ! ^ $(whoami) " + '"' + "원문" + '"'
    output = json.loads(cli._call(cli.checked_config({"executable": str(batch)}), list(cli.FLAGS), prompt=prompt))
    assert output["received"] == prompt and prompt not in output["args"]
    assert output["tools_disabled"] and not Path(output["cwd"]).exists()


def test_cli_prepare_without_copilot_or_network(project, tmp_path):
    document = trace.knowledge(project)["documents"][0]["document_id"]
    result_path = tmp_path / "result.json"
    assert main(["copilot-prepare", "--project", str(project), "--documents", document, "--result-json", str(result_path)]) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["llm_calls"] == 0 and result["count"] >= 1
