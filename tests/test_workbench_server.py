"""Local API contracts, source immutability, frozen references and saved choices."""
import base64
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd
import pytest

from battery_insight.workbench_server import MAX_BODY, STATE_FILES, Workbench, history_entry, make_server
from test_dual_maps_integration import dual_workspace, varied_fake_pipeline


@pytest.fixture
def workspace_controller(tmp_path, monkeypatch):
    config, inbox, registry, rows, output = dual_workspace(tmp_path)
    calls, changes = varied_fake_pipeline(monkeypatch, rows)
    controller = Workbench(config, output)
    return controller, config, inbox, registry, rows, calls, changes


def test_display_preferences_and_calculation_selections_are_independent(workspace_controller):
    controller, *_ = workspace_controller
    original = copy.deepcopy(controller.analysis["graphs"]["electrochem"])
    profile = controller.save_profile("설계만 표시", controller.options, ["network"])
    assert profile["profile"]["visible_modules"] == ["network"]
    assert controller.analysis["graphs"]["electrochem"] == original
    options = copy.deepcopy(controller.options)
    options["metric_ids"] = options["metric_ids"][:1]
    options["min_overlap"] = 1
    result = controller.compare(options)
    assert len(result["graphs"]["electrochem"]["metrics"]) == 1
    assert len(result["graphs"]["electrochem"]["edges"]) == 3
    options["metric_ids"] = []
    result = controller.compare(options)
    assert result["graphs"]["electrochem"]["edges"] == []
    assert len(result["graphs"]["electrochem"]["nodes"]) == 3


def test_profile_roundtrip_and_raw_source_immutability(workspace_controller):
    controller, config, inbox, registry, rows, *_ = workspace_controller
    original = {p: p.read_bytes() for p in [config, registry, *(inbox.glob("*.csv"))]}
    chosen = copy.deepcopy(controller.options)
    chosen["enabled_modules"] = ["network", "crossview"]
    chosen["design_field_ids"] = []
    saved = controller.save_profile("내 비교", chosen, ["network", "crossview"])["profile"]
    assert saved["options"]["design_field_ids"] == []
    reloaded = Workbench(config, controller.output)
    assert reloaded.profiles() == [saved]
    assert all(p.read_bytes() == content for p, content in original.items())
    assert not (controller.output / "registry" / "relations.json").exists()


def test_baseline_preserves_reference_and_history_changes_are_explained(workspace_controller):
    controller, *_ = workspace_controller
    saved = controller.save_baseline()
    reference = saved["reference"]
    assert reference["summary"] == controller.options["summary"]
    assert reference["metrics"]
    assert controller.options["scale_mode"] == "fixed"
    assert controller.bundle["baseline"]["reference_id"] == reference["reference_id"]
    assert (controller.output / "references" / (reference["reference_id"] + ".json")).exists()
    count = len(controller.history())
    controller.compare(controller.options)
    assert len(controller.history()) == count
    changed = copy.deepcopy(controller.options)
    changed["summary"] = "change"
    with pytest.raises(ValueError):
        controller.compare(changed)
    assert controller.options["summary"] == reference["summary"]


def test_registration_is_previewed_and_writes_only_a_managed_copy(workspace_controller):
    controller, config, inbox, registry, rows, *_ = workspace_controller
    original_registry, original_config = registry.read_bytes(), config.read_bytes()
    incoming = [dict(rows[0], label="변경한 이름", cathode_active_mass_loading_mg_cm2=12)]
    preview = controller.validate_registration(incoming)
    assert preview["validation"]["valid"]
    assert registry.read_bytes() == original_registry
    with pytest.raises(ValueError, match="preview"):
        controller.apply_registration(incoming, expected_registry_hash="stale")
    applied = controller.apply_registration(incoming, expected_registry_hash=preview["registry_hash"])
    assert applied["updated"] == 1
    assert registry.read_bytes() == original_registry and config.read_bytes() == original_config
    managed = Path(applied["registration_path"])
    assert managed.is_relative_to(controller.output)
    assert pd.read_csv(managed).iloc[0]["label"] == "변경한 이름"
    reloaded = Workbench(config, controller.output)
    assert reloaded.config_path != config
    assert reloaded.bundle["experiments"][0]["label"] == "변경한 이름"


def test_registration_validation_does_not_accept_unknown_or_escaping_raw(workspace_controller):
    controller, _, _, registry, rows, *_ = workspace_controller
    before = registry.read_bytes()
    invalid = [dict(rows[0], raw_path="../../outside.csv")]
    result = controller.validate_registration(invalid)
    assert not result["validation"]["valid"]
    assert "inside inbox" in result["validation"]["errors"][0]
    assert registry.read_bytes() == before
    incomplete = [dict(rows[0], cam_wt_pct=80)]
    assert not controller.validate_registration(incomplete)["validation"]["valid"]
    duplicate = [rows[0], rows[0]]
    assert not controller.validate_registration(duplicate)["validation"]["valid"]


def test_history_distinguishes_population_scale_and_measurement_changes():
    base = {"experiment_ids": ["A", "B"], "values": {'["electrochem", "A", "m"]': 10.},
            "edges": {'["electrochem", "A", "B"]': {"similarity": .9, "coverage": 1}},
            "scales": {"C": [{"metric_id": "m", "center": 10, "scale": 1}]}, "options": {}}
    current = copy.deepcopy(base)
    current["experiment_ids"].append("C")
    current["scales"]["C"][0]["scale"] = 2
    current["edges"]['["electrochem", "A", "B"]']["similarity"] = .95
    difference = history_entry(base, current)
    assert difference["changed_values"] == 0
    assert difference["scale_changed"] and difference["changed_edges"] == 1
    assert difference["added_experiments"] == ["C"]
    assert "standardization_changed" in difference["reason"]


def test_local_http_mutations_require_same_origin_and_valid_json(workspace_controller):
    controller, *_ = workspace_controller
    server = make_server(controller, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(address + "/api/state") as response:
            state = json.load(response)
        assert state["capabilities"]["server"] is True
        options = copy.deepcopy(state["analysis"]["options"])
        options["metric_ids"] = []
        options["design_field_ids"] = []
        request = Request(address + "/api/compare", data=json.dumps({"options": options}).encode(),
                          headers={"Content-Type": "application/json", "Origin": address})
        with urlopen(request) as response:
            assert json.load(response)["graphs"]["electrochem"]["edges"] == []
        foreign = Request(address + "/api/compare", data=b"{}", headers={"Content-Type": "application/json", "Origin": "https://example.com"})
        with pytest.raises(HTTPError) as error:
            urlopen(foreign)
        assert error.value.code == 403
        with pytest.raises(HTTPError) as error:
            urlopen(address + "/../registry/experiments.csv")
        assert error.value.code == 404
        with pytest.raises(HTTPError) as error:
            urlopen(Request(address + "/api/compare", data=b"{}", headers={"Content-Type": "text/plain"}))
        assert error.value.code == 400
        for invalid in ("NaN", "Infinity", "1e309"):
            data = ('{"options":{"life_max":' + invalid + '}}').encode()
            with pytest.raises(HTTPError) as error:
                urlopen(Request(address + "/api/compare", data=data, headers={"Content-Type": "application/json"}))
            assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_csv_mapping_preview_preserves_ids_and_requires_explicit_apply(workspace_controller):
    controller, _, _, registry, rows, *_ = workspace_controller
    columns = dict(rows[0])
    columns["시험ID"] = columns.pop("experiment_id")
    upload = pd.DataFrame([columns]).to_csv(index=False).encode("utf-8-sig")
    before = registry.read_bytes()
    preview = controller.preview_upload(upload, "registrations.csv", mapping={"experiment_id": "시험ID"})
    assert preview["validation"]["valid"]
    assert preview["rows"][0]["experiment_id"] == rows[0]["experiment_id"]
    assert registry.read_bytes() == before


def published_snapshot(controller):
    paths = [controller.output / name for name in STATE_FILES]
    paths += [path.with_name(path.name + ".tmp") for path in paths]
    paths += list((controller.output / "references").glob("*.json"))
    paths += list((controller.output / "references").glob("*.json.tmp"))
    paths += list((controller.output / "registry" / "versions").glob("*.csv"))
    return {str(path.relative_to(controller.output)): path.read_bytes() for path in paths if path.exists()}


def memory_snapshot(controller):
    return copy.deepcopy({"bundle": controller.bundle, "analysis": controller.analysis, "options": controller.options,
                          "config_path": controller.config_path, "last_error": controller.last_error})


def fail_partial_renderer(monkeypatch):
    def failed_render(bundle, analysis, destination):
        target = Path(destination)
        (target / "data.json").write_text("partially replaced report", encoding="utf-8")
        (target / "index.html.tmp").write_text("unfinished page", encoding="utf-8")
        raise OSError("simulated renderer file failure")
    monkeypatch.setattr("battery_insight.workbench_report.write_workbench_report", failed_render)


def test_refresh_renderer_failure_restores_published_files_and_memory(workspace_controller, monkeypatch):
    controller, _, inbox, _, rows, _, changes = workspace_controller
    before_files, before_memory = published_snapshot(controller), memory_snapshot(controller)
    changes[rows[1]["cell_id"]] = 8
    raw = inbox / rows[1]["raw_path"]
    raw.write_text("externally updated source measurement\n", encoding="utf-8")
    new_raw = raw.read_bytes()
    fail_partial_renderer(monkeypatch)
    with pytest.raises(OSError, match="renderer file failure"):
        controller.refresh()
    assert published_snapshot(controller) == before_files
    assert memory_snapshot(controller) == before_memory
    assert raw.read_bytes() == new_raw  # Rollback never rewinds source data.


def test_compare_file_failure_restores_options_history_and_results(workspace_controller, monkeypatch):
    controller, *_ = workspace_controller
    before_files, before_memory = published_snapshot(controller), memory_snapshot(controller)
    from battery_insight import workbench_server
    original_write = workbench_server._atomic_json
    def failed_write(path, value):
        if Path(path).name == "workbench_options.json":
            Path(path).write_bytes(b"partial options")
            raise OSError("simulated options file error")
        return original_write(path, value)
    monkeypatch.setattr(workbench_server, "_atomic_json", failed_write)
    options = copy.deepcopy(controller.options)
    options.update(metric_ids=options["metric_ids"][:1], min_overlap=1)
    with pytest.raises(OSError, match="options file error"):
        controller.compare(options)
    assert published_snapshot(controller) == before_files
    assert memory_snapshot(controller) == before_memory


@pytest.mark.parametrize("already_managed", [False, True])
def test_registration_refresh_failure_restores_managed_state_exactly(workspace_controller, monkeypatch, already_managed):
    controller, config, _, registry, rows, *_ = workspace_controller
    source_config, source_registry = config.read_bytes(), registry.read_bytes()
    if already_managed:
        first = [dict(rows[0], label="Committed managed label")]
        preview = controller.validate_registration(first)
        controller.apply_registration(first, expected_registry_hash=preview["registry_hash"])
    before_files, before_memory = published_snapshot(controller), memory_snapshot(controller)
    incoming = [dict(rows[0], label="Uncommitted label", cathode_active_mass_loading_mg_cm2=20)]
    preview = controller.validate_registration(incoming)
    fail_partial_renderer(monkeypatch)
    with pytest.raises(OSError, match="renderer file failure"):
        controller.apply_registration(incoming, expected_registry_hash=preview["registry_hash"])
    assert published_snapshot(controller) == before_files
    assert memory_snapshot(controller) == before_memory
    assert config.read_bytes() == source_config and registry.read_bytes() == source_registry


@pytest.mark.parametrize("existing_reference", [False, True])
def test_failed_baseline_save_restores_previous_reference_or_absence(workspace_controller, monkeypatch, existing_reference):
    controller, *_ = workspace_controller
    if existing_reference:
        controller.save_baseline()
    before_files, before_memory = published_snapshot(controller), memory_snapshot(controller)
    options = copy.deepcopy(controller.options)
    options.update(metric_ids=options["metric_ids"][:1], min_overlap=1)
    fail_partial_renderer(monkeypatch)
    with pytest.raises(OSError, match="renderer file failure"):
        controller.save_baseline(options)
    assert published_snapshot(controller) == before_files
    assert memory_snapshot(controller) == before_memory


def test_failed_first_refresh_does_not_leave_current_state_files(tmp_path, monkeypatch):
    config, _, _, rows, output = dual_workspace(tmp_path)
    varied_fake_pipeline(monkeypatch, rows)
    controller = Workbench(config, output, initialize=False)
    before_memory = memory_snapshot(controller)
    def failed_render(bundle, analysis, destination):
        Path(destination).mkdir(parents=True, exist_ok=True)
        (Path(destination) / "index.html").write_text("uncommitted", encoding="utf-8")
        raise OSError("first report failed")
    monkeypatch.setattr("battery_insight.workbench_report.write_workbench_report", failed_render)
    with pytest.raises(OSError, match="first report failed"):
        controller.refresh()
    assert published_snapshot(controller) == {}
    assert memory_snapshot(controller) == before_memory


def test_fixed_scale_history_ignores_reference_population_count_changes():
    base = {"experiment_ids": ["A", "B"], "values": {}, "edges": {},
            "scales": {"C": [{"metric_id": "m", "center": 10, "scale": 1, "method": "fixed:median_mad", "n_entities": 2}]}, "options": {}}
    current = copy.deepcopy(base)
    current["experiment_ids"].append("C")
    current["scales"]["C"][0]["n_entities"] = 3
    difference = history_entry(base, current)
    assert difference["scale_changed"] is False
    assert "population_changed" in difference["reason"]
    assert "standardization_changed" not in difference["reason"]


def test_current_scale_mode_does_not_require_old_reference_file(workspace_controller):
    controller, *_ = workspace_controller
    options = copy.deepcopy(controller.options)
    options.update(scale_mode="current", reference_id="nonexistent_old_reference")
    payload = controller.compare(options)
    assert payload["metadata"]["reference"]["scale_mode"] == "current"


def test_old_fixed_profile_keeps_active_reference_distinct_from_latest_saved(workspace_controller):
    controller, config, *_ = workspace_controller
    first = controller.save_baseline()["reference"]
    first_options = copy.deepcopy(controller.options)
    second_options = copy.deepcopy(first_options)
    second_options.update(metric_ids=second_options["metric_ids"][:1], min_overlap=1)
    latest = controller.save_baseline(second_options)["reference"]
    assert first["reference_id"] != latest["reference_id"]
    profile = controller.save_profile("Earlier fixed reference", first_options, ["network", "history"])["profile"]
    assert controller.bundle["active_reference"]["reference_id"] == latest["reference_id"]  # Validation has no selection side effects.
    selected = controller.compare(profile["options"])
    assert selected["active_reference"]["reference_id"] == first["reference_id"]
    assert selected["metadata"]["reference"]["reference_id"] == first["reference_id"]
    assert controller.bundle["baseline"]["reference_id"] == latest["reference_id"]
    assert controller.bundle["active_reference"]["reference_id"] == first["reference_id"]
    controller.refresh()
    assert controller.bundle["baseline"]["reference_id"] == latest["reference_id"]
    assert controller.analysis["active_reference"] == controller.bundle["active_reference"] == first
    stored_bundle = json.loads((controller.output / "dashboard" / "data.json").read_text(encoding="utf-8"))
    stored_analysis = json.loads((controller.output / "dashboard" / "analysis.json").read_text(encoding="utf-8"))
    assert stored_bundle["active_reference"] == stored_analysis["active_reference"] == first
    reloaded = Workbench(config, controller.output)
    assert reloaded.options["reference_id"] == first["reference_id"]
    assert reloaded.bundle["active_reference"] == first
    assert reloaded.bundle["baseline"] == latest
    current = copy.deepcopy(reloaded.options)
    current["scale_mode"] = "current"
    result = reloaded.compare(current)
    assert result["active_reference"] is None and reloaded.bundle["active_reference"] is None
    assert reloaded.bundle["baseline"] == latest


def test_excel_sheet_selection_cannot_validate_existing_registry_by_accident(workspace_controller, monkeypatch):
    controller, *_ = workspace_controller
    monkeypatch.setattr("battery_insight.workbench_data.parse_registration_upload", lambda *args: {
        "rows": [], "columns": [], "sheets": ["first", "second"], "selected_sheet": None, "requires_sheet_selection": True})
    result = controller.preview_upload(b"placeholder", "registry.xlsx")
    assert result["validation"]["valid"] is False
    assert result["validation"]["requires_sheet_selection"] is True
    assert "worksheet" in result["validation"]["errors"][0]


def test_upload_body_budget_includes_base64_overhead():
    assert MAX_BODY == 16 * 1024 * 1024
    assert MAX_BODY > 4 * ((10 * 1024 * 1024 + 2) // 3) + 2048


def test_transaction_rejects_any_target_outside_output(workspace_controller):
    controller, config, *_ = workspace_controller
    original = config.read_bytes()
    with pytest.raises(ValueError, match="inside the output"):
        with controller._transaction(extra_paths=[config]):
            raise AssertionError("must not enter transaction")
    assert config.read_bytes() == original


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
@pytest.mark.parametrize("stage", ["headers", "body"])
def test_closed_client_response_is_not_sent_twice(error, stage):
    server = make_server(None, 0)
    statuses, bodies = [], []
    def fail():
        raise error("client disconnected after commit")
    def write(content):
        bodies.append(content)
        fail()
    fake = SimpleNamespace(send_response=statuses.append, send_header=lambda *args: None,
                           end_headers=fail if stage == "headers" else lambda: None,
                           wfile=SimpleNamespace(write=write), close_connection=False)
    try:
        server.RequestHandlerClass.send_json(fake, {"saved": True})
        assert statuses == [200]
        assert fake.close_connection is True
        assert len(bodies) == (1 if stage == "body" else 0)
    finally:
        server.server_close()


def test_response_serialization_failure_is_not_swallowed_as_disconnect():
    server = make_server(None, 0)
    statuses = []
    fake = SimpleNamespace(send_response=statuses.append)
    try:
        with pytest.raises(ValueError):
            server.RequestHandlerClass.send_json(fake, {"invalid": float("nan")})
        assert statuses == []
    finally:
        server.server_close()
