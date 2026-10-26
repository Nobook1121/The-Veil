"""剧本世界树 / 图谱编辑 API 的确定性测试（设计文档 §8.5）。"""
from __future__ import annotations

import json
import time

from trpg_server.app_factory import create_app

CSRF = {"X-CSRF-Token": "csrf"}


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    return create_app({
        "TESTING": True,
        "USER_MANAGER": FakeManager(role),
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "SCENARIOS_DIR": tmp_path / "scenarios",
        "CHARACTERS_DIR": tmp_path / "characters",
        "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
        "HISTORY_DIR": tmp_path / "history",
        "NARRATIVE_JOBS_DIR": tmp_path / "narrative-jobs",
    })


def _login(client, user_id=1, role="ADMIN"):
    with client.session_transaction() as session:
        session.update(user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf")


def _bundle(revision=2):
    return {
        "scenario_id": "7",
        "scenario_version": "1.0.0",
        "topology": "hub",
        "nodes": [
            {"id": "hall", "kind": "location", "title": "大厅", "access": "free", "scene_role": "hub"},
            {"id": "room", "kind": "location", "title": "房间"},
        ],
        "edges": [
            {"id": "edge_hall_room", "from": "hall", "to": "room", "type": "adjacent"},
        ],
        "meta": {"revision": revision},
    }


def _write_scenario(tmp_path, *, import_mode="edit", owner_id=1, bundle=None, modules=None):
    storage = tmp_path / "scenarios" / "scenario-7"
    storage.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "id": 7,
        "title": "测试剧本",
        "owner_id": owner_id,
        "import_mode": import_mode,
        "modules": modules if modules is not None else [],
    }
    (storage / "scenario.json").write_text(json.dumps(descriptor, ensure_ascii=False), encoding="utf-8")
    if bundle is not None:
        (storage / "narrative.json").write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return storage


def _get(client):
    return client.get("/api/scenarios/7/narrative")


def _patch_node(client, node_id="room", **body):
    payload = {"base_revision": 2}
    payload.update(body)
    return client.patch(f"/api/scenarios/7/narrative/nodes/{node_id}", json=payload, headers=CSRF)


def test_visibility_review_clears_flag_and_keeps_visibility(tmp_path):
    # §8.7：接受自动判定只清除 needs_review，不改变可见性。
    bundle = _bundle()
    bundle["nodes"][1]["visibility"] = "hidden"
    bundle["nodes"][1]["needs_review"] = True
    bundle["nodes"][1]["visibility_source"] = "auto"
    _write_scenario(tmp_path, bundle=bundle)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/scenarios/7/narrative/visibility/review",
        json={"target": "node:room", "base_revision": 2},
        headers=CSRF,
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["revision"] == 3
    stored = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    node = next(item for item in stored["nodes"] if item["id"] == "room")
    assert node["visibility"] == "hidden"
    assert "needs_review" not in node
    assert node["visibility_source"] == "author"


def test_get_narrative_returns_bundle_and_permissions(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _get(client)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert [node["id"] for node in data["bundle"]["nodes"]] == ["hall", "room"]
    assert data["revision"] == 2
    assert data["permissions"] == {"can_view": True, "can_edit": True, "readonly": False}
    assert data["summary"]["counts"]["nodes"] == 2


def test_get_narrative_requires_generated_bundle(tmp_path):
    _write_scenario(tmp_path)
    client = _app(tmp_path).test_client()
    _login(client)

    response = _get(client)

    assert response.status_code == 404


def test_patch_node_updates_fields_and_bumps_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _patch_node(client, title="密室", visibility="kp_only", kp_notes="仅 KP")

    assert response.status_code == 200
    assert response.get_json()["data"]["revision"] == 3
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    node = next(item for item in saved["nodes"] if item["id"] == "room")
    assert node["title"] == "密室"
    assert node["visibility"] == "kp_only"
    assert node["kp_notes"] == "仅 KP"
    assert saved["meta"]["revision"] == 3


def test_patch_node_saves_reveal_when(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _patch_node(
        client,
        visibility="hidden",
        reveal_when={"skill": "侦查", "difficulty": 2},
    )

    assert response.status_code == 200
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    node = next(item for item in saved["nodes"] if item["id"] == "room")
    assert node["visibility"] == "hidden"
    assert node["reveal_when"] == {"skill": "侦查", "difficulty": 2}


def test_patch_edge_saves_visibility_and_reveal_when(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/edges/edge_hall_room",
        json={"base_revision": 2, "visibility": "hidden", "reveal_when": {"clue": "clue_key"}},
        headers=CSRF,
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["revision"] == 3
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    edge = next(item for item in saved["edges"] if item["id"] == "edge_hall_room")
    assert edge["visibility"] == "hidden"
    assert edge["reveal_when"] == {"clue": "clue_key"}
    assert edge["from"] == "hall"
    assert edge["to"] == "room"


def test_patch_node_rejects_stale_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/nodes/room",
        json={"base_revision": 1, "title": "新"},
        headers=CSRF,
    )

    assert response.status_code == 409
    assert "revision 2" in response.get_data(as_text=True)


def test_patch_requires_base_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch("/api/scenarios/7/narrative/nodes/room", json={"title": "新"}, headers=CSRF)

    assert response.status_code == 409


def test_patch_edge_updates_type(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/edges/edge_hall_room",
        json={"base_revision": 2, "hard": True, "label": "门"},
        headers=CSRF,
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["edge"]["hard"] is True


def test_put_narrative_replaces_bundle(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)
    bundle = _bundle()
    bundle["nodes"].append({"id": "ending_a", "kind": "ending", "title": "结局 A"})

    response = client.put(
        "/api/scenarios/7/narrative",
        json={"base_revision": 2, "bundle": bundle},
        headers=CSRF,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["revision"] == 3
    assert any(node["id"] == "ending_a" for node in data["bundle"]["nodes"])
    assert data["bundle"]["scenario_id"] == "7"


def test_put_narrative_rejects_empty_nodes(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.put(
        "/api/scenarios/7/narrative",
        json={"base_revision": 2, "bundle": {"nodes": [], "edges": []}},
        headers=CSRF,
    )

    assert response.status_code == 400


def test_validate_returns_report_and_order_simulation(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/scenarios/7/narrative/validate",
        json={"all_orders": True},
        headers=CSRF,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["topology"] == "hub"
    assert isinstance(data["issues"], list)
    assert data["order_simulation"]["ok"] is True


def test_topology_override_marks_author_source(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/topology",
        json={"base_revision": 2, "topology": "linear"},
        headers=CSRF,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["topology"] == "linear"
    assert data["source"] == "author"


def test_topology_override_rejects_unknown_value(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/topology",
        json={"base_revision": 2, "topology": "free"},
        headers=CSRF,
    )

    assert response.status_code == 400


def test_direct_import_is_read_only(tmp_path):
    _write_scenario(tmp_path, import_mode="direct", bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    assert _get(client).get_json()["data"]["permissions"]["readonly"] is True
    response = _patch_node(client, title="新")
    assert response.status_code == 403


def test_non_owner_cannot_edit(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path, role="USER").test_client()
    _login(client, user_id=2, role="USER")

    response = _patch_node(client, title="新")

    assert response.status_code == 403


def _create_edge(client, **body):
    payload = {"base_revision": 2, "from": "hall", "to": "room", "type": "portal"}
    payload.update(body)
    return client.post("/api/scenarios/7/narrative/edges", json=payload, headers=CSRF)


def test_create_edge_appends_and_bumps_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _create_edge(client, label="木门")

    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["revision"] == 3
    assert data["edge"]["label"] == "木门"
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    assert any(edge.get("label") == "木门" for edge in saved["edges"])


def test_create_edge_rejects_unknown_endpoint(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _create_edge(client, to="ghost")

    assert response.status_code == 400


def test_create_edge_rejects_duplicate(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _create_edge(client, type="adjacent")

    assert response.status_code == 409


def test_delete_edge_removes_and_bumps_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.delete(
        "/api/scenarios/7/narrative/edges/edge_hall_room",
        json={"base_revision": 2},
        headers=CSRF,
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["revision"] == 3
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    assert saved["edges"] == []


def test_delete_edge_requires_base_revision(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.delete("/api/scenarios/7/narrative/edges/edge_hall_room", headers=CSRF)

    assert response.status_code == 409


def _trigger_bundle():
    bundle = _bundle()
    bundle["assets"] = [{"id": "asset_pic", "kind": "image", "url": "/assets/pic.png", "alt": "旧图"}]
    bundle["triggers"] = [
        {
            "id": "trg_intro",
            "label": "开场",
            "binding": {"kind": "enter", "location_id": "hall"},
            "attachments": [{"asset_id": "asset_pic", "kind": "image", "visibility": "player_visible"}],
        }
    ]
    return bundle


def test_list_triggers_returns_specs_and_assets(tmp_path):
    _write_scenario(tmp_path, bundle=_trigger_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/scenarios/7/narrative/triggers")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["triggers"][0]["id"] == "trg_intro"
    assert data["assets"][0]["alt"] == "旧图"
    assert data["revision"] == 2


def test_create_and_test_trigger(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    created = client.post(
        "/api/scenarios/7/narrative/triggers",
        json={
            "base_revision": 2,
            "trigger": {
                "id": "trg_lamp",
                "label": "油灯",
                "binding": {"kind": "enter", "location_id": "room"},
                "content": "灯芯跳动。",
                "attachments": [
                    {"kind": "richtext", "body": "**灯芯**跳动。", "visibility": "player_visible"}
                ],
            },
        },
        headers=CSRF,
    )
    assert created.status_code == 201
    assert created.get_json()["data"]["revision"] == 3

    preview = client.post("/api/scenarios/7/narrative/triggers/trg_lamp/test", json={}, headers=CSRF)
    assert preview.status_code == 200
    data = preview.get_json()["data"]
    assert data["ok"] is True
    assert data["resolved_target"] == "room"
    assert data["attachments"][0]["body"] == "**灯芯**跳动。"


def test_trigger_preview_reports_missing_binding(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    client.post(
        "/api/scenarios/7/narrative/triggers",
        json={
            "base_revision": 2,
            "trigger": {"id": "trg_ghost", "binding": {"kind": "enter", "location_id": "ghost"}},
        },
        headers=CSRF,
    )
    preview = client.post("/api/scenarios/7/narrative/triggers/trg_ghost/test", json={}, headers=CSRF)

    data = preview.get_json()["data"]
    assert data["ok"] is False
    assert data["resolved_target"] == "ghost"


def test_update_and_delete_trigger(tmp_path):
    _write_scenario(tmp_path, bundle=_trigger_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    updated = client.put(
        "/api/scenarios/7/narrative/triggers/trg_intro",
        json={"base_revision": 2, "label": "新开场", "once": False, "cooldown": 3},
        headers=CSRF,
    )
    assert updated.status_code == 200
    assert updated.get_json()["data"]["trigger"]["label"] == "新开场"
    assert updated.get_json()["data"]["trigger"]["once"] is False

    deleted = client.delete(
        "/api/scenarios/7/narrative/triggers/trg_intro",
        json={"base_revision": 3},
        headers=CSRF,
    )
    assert deleted.status_code == 200
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    assert saved["triggers"] == []


def test_patch_asset_metadata(tmp_path):
    _write_scenario(tmp_path, bundle=_trigger_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.patch(
        "/api/scenarios/7/narrative/assets/asset_pic",
        json={"base_revision": 2, "alt": "新图", "caption": "门后的画"},
        headers=CSRF,
    )

    assert response.status_code == 200
    asset = response.get_json()["data"]["asset"]
    assert asset["alt"] == "新图"
    assert asset["caption"] == "门后的画"


def test_reclassify_visibility_preview_then_apply(tmp_path):
    bundle = _bundle()
    bundle["nodes"].append({"id": "secret", "kind": "clue", "title": "真相", "visibility": "public"})
    _write_scenario(tmp_path, bundle=bundle)
    client = _app(tmp_path).test_client()
    _login(client)

    preview = client.post(
        "/api/scenarios/7/narrative/visibility/reclassify",
        json={"base_revision": 2},
        headers=CSRF,
    )
    assert preview.status_code == 200
    data = preview.get_json()["data"]
    assert data["applied"] is False
    assert data["changed"] == 1
    assert data["changes"][0]["id"] == "secret"
    assert data["changes"][0]["to"] == "kp_only"
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    node = next(item for item in saved["nodes"] if item["id"] == "secret")
    assert node["visibility"] == "public"

    applied = client.post(
        "/api/scenarios/7/narrative/visibility/reclassify",
        json={"base_revision": 2, "apply": True},
        headers=CSRF,
    )
    assert applied.status_code == 200
    result = applied.get_json()["data"]
    assert result["applied"] is True
    assert result["revision"] == 3
    node = next(item for item in result["nodes"] if item["id"] == "secret")
    assert node["visibility"] == "kp_only"
    assert node["visibility_source"] == "auto"


def test_reclassify_visibility_keeps_author_override(tmp_path):
    bundle = _bundle()
    bundle["nodes"].append({"id": "secret", "kind": "clue", "title": "真相", "visibility": "kp_only", "visibility_source": "author"})
    _write_scenario(tmp_path, bundle=bundle)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/scenarios/7/narrative/visibility/reclassify",
        json={"base_revision": 2},
        headers=CSRF,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["changed"] == 0
    assert data["auto"] == 2


def test_narrative_start_get_and_put(tmp_path):
    bundle = _bundle()
    bundle["start"] = {"location_id": "hall", "opening_mode": "fixed", "public_intro": ["旧开场"], "kp_truth": "秘密"}
    _write_scenario(tmp_path, bundle=bundle)
    client = _app(tmp_path).test_client()
    _login(client)

    read = client.get("/api/scenarios/7/narrative/start")
    assert read.status_code == 200
    payload = read.get_json()["data"]
    assert payload["start"]["location_id"] == "hall"
    assert payload["start"]["opening_mode"] == "fixed"
    assert {item["id"] for item in payload["locations"]} == {"hall", "room"}

    saved = client.put(
        "/api/scenarios/7/narrative/start",
        json={
            "base_revision": 2,
            "start": {
                "location_id": "room",
                "opening_mode": "hybrid",
                "public_intro": ["第一段", "第二段"],
                "kp_truth": "新的秘密",
                "allow_enter_triggers": False,
            },
        },
        headers=CSRF,
    )
    assert saved.status_code == 200
    data = saved.get_json()["data"]
    assert data["revision"] == 3
    assert data["start"]["location_id"] == "room"
    assert data["start"]["opening_mode"] == "hybrid"
    assert data["start"]["public_intro"] == ["第一段", "第二段"]
    assert data["start"]["allow_enter_triggers"] is False


def _wait_generate_job(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/scenarios/7/narrative/jobs/{job_id}")
        assert response.status_code == 200
        data = response.get_json()["data"]
        if data["job"]["status"] in {"done", "failed"}:
            return data
        time.sleep(0.05)
    raise AssertionError("generation job did not finish in time")


def test_generate_from_modules_returns_preview_without_saving(tmp_path):
    modules = [
        {"id": "m1", "module_type": "scene", "title": "大厅", "content": "线索：血书"},
        {"id": "m2", "module_type": "scene", "title": "书房", "content": "NPC：管家"},
    ]
    _write_scenario(tmp_path, bundle=_bundle(), modules=modules)
    client = _app(tmp_path).test_client()
    _login(client)

    started = client.post(
        "/api/scenarios/7/narrative/generate",
        json={"options": {"source": "modules", "topology": "linear"}},
        headers=CSRF,
    )
    assert started.status_code == 202
    job_id = started.get_json()["data"]["job_id"]

    data = _wait_generate_job(client, job_id)
    assert data["job"]["status"] == "done"
    result = data["result"]
    assert result["topology"]["topology"] == "linear"
    assert result["topology"]["source"] == "author"
    assert result["base_revision"] == 2
    titles = {node["title"] for node in result["bundle"]["nodes"]}
    assert {"大厅", "书房"} <= titles
    assert result["diff"]["counts"]["nodes_added"] > 0

    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    assert saved["meta"]["revision"] == 2
    assert [node["id"] for node in saved["nodes"]] == ["hall", "room"]


def test_generate_from_premise_builds_skeleton(tmp_path):
    _write_scenario(tmp_path, modules=[])
    client = _app(tmp_path).test_client()
    _login(client)

    started = client.post(
        "/api/scenarios/7/narrative/generate",
        json={
            "options": {
                "source": "premise",
                "premise": "调查山中古堡",
                "topology": "hybrid",
                "acts": 2,
                "scenes_per_act": 2,
                "endings": 1,
                "clocks": 1,
                "branch": "medium",
            }
        },
        headers=CSRF,
    )
    assert started.status_code == 202
    data = _wait_generate_job(client, started.get_json()["data"]["job_id"])
    result = data["result"]

    kinds = {node["kind"] for node in result["bundle"]["nodes"]}
    assert {"act", "area", "location", "ending"} <= kinds
    assert len(result["bundle"]["clocks"]) == 1
    assert any(item["code"] == "generate.premise_skeleton" for item in result["warnings"])
    assert result["base_revision"] == 0


def test_generate_with_ai_fill_without_platform_warns(tmp_path):
    _write_scenario(tmp_path, modules=[])
    client = _app(tmp_path).test_client()
    _login(client)

    started = client.post(
        "/api/scenarios/7/narrative/generate",
        json={"options": {"source": "premise", "premise": "山中古堡", "ai_fill": True}},
        headers=CSRF,
    )
    assert started.status_code == 202
    data = _wait_generate_job(client, started.get_json()["data"]["job_id"])

    assert data["job"]["status"] == "done"
    result = data["result"]
    assert any(item["code"] == "generate.ai_fill_unavailable" for item in result["warnings"])
    assert result["bundle"]["meta"]["ai_fill"]["filled"] == 0


def test_asset_thumb_falls_back_to_original_image(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    assets_dir = tmp_path / "scenarios" / "scenario-7" / "assets"
    assets_dir.mkdir(parents=True)
    image = assets_dir / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "scenarios" / "scenario-7" / "assets.json").write_text(
        json.dumps(
            [
                {
                    "type": "image",
                    "path": "assets/pic.png",
                    "url": "/assets/scenarios/scenario-7/assets/pic.png",
                    "alt": "",
                    "mime": "image/png",
                    "size": 8,
                    "hash": "asset-hash",
                }
            ]
        ),
        encoding="utf-8",
    )
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.get("/api/scripts/7/assets/asset-hash/thumb")
    missing = client.get("/api/scripts/7/assets/unknown/thumb")

    assert response.status_code == 200
    assert response.data.startswith(b"\x89PNG")
    assert response.headers["Cache-Control"].startswith("private")
    assert missing.status_code == 404


def test_suggest_keywords_returns_new_candidates(tmp_path):
    modules = [
        {
            "id": "m1",
            "module_type": "scene",
            "title": "古堡大厅",
            "content": "调查员反复提到「银钥匙」，并注意到墙上的纹章。",
            "keywords": ["大厅"],
        }
    ]
    _write_scenario(tmp_path, bundle=_bundle(), modules=modules)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post("/api/scenarios/7/knowledge/suggest-keywords", json={"limit": 8}, headers=CSRF)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["count"] == 1
    suggestion = data["suggestions"][0]
    assert suggestion["module_id"] == "m1"
    assert "银钥匙" in suggestion["suggested"]
    assert "大厅" not in suggestion["suggested"]


def test_suggest_node_keywords_extracts_from_body(tmp_path):
    bundle = _bundle()
    bundle["nodes"][1]["keywords"] = ["房间"]
    bundle["nodes"][1]["body"] = "调查员进入「青铜镜」前反复查看青铜镜，镜面上刻着纹章。"
    _write_scenario(tmp_path, bundle=bundle)
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/scenarios/7/knowledge/suggest-keywords",
        json={"target": "node", "node_ids": ["room"], "limit": 8},
        headers=CSRF,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["count"] == 1
    suggestion = data["suggestions"][0]
    assert suggestion["node_id"] == "room"
    assert suggestion["current"] == ["房间"]
    keys = [item["key"] for item in suggestion["suggested"]]
    assert "青铜镜" in keys
    assert "房间" not in keys
    candidate = next(item for item in suggestion["suggested"] if item["key"] == "青铜镜")
    assert candidate["source"] in {"quoted", "body"}
    assert 0 < candidate["confidence"] <= 1


def test_patch_node_persists_keywords_and_scope(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = _patch_node(
        client,
        keywords=["老陈"],
        aliases=["陈叔"],
        negative_keywords=["假的"],
        scope="global",
        constant=True,
    )

    assert response.status_code == 200
    saved = json.loads((tmp_path / "scenarios" / "scenario-7" / "narrative.json").read_text(encoding="utf-8"))
    node = next(item for item in saved["nodes"] if item["id"] == "room")
    assert node["keywords"] == ["老陈"]
    assert node["aliases"] == ["陈叔"]
    assert node["negative_keywords"] == ["假的"]
    assert node["scope"] == "global"
    assert node["constant"] is True


def test_generate_is_blocked_for_direct_import(tmp_path):
    _write_scenario(tmp_path, import_mode="direct", bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/scenarios/7/narrative/generate",
        json={"options": {"source": "document"}},
        headers=CSRF,
    )

    assert response.status_code == 403


def test_generate_job_is_owner_scoped(tmp_path):
    _write_scenario(tmp_path, bundle=_bundle())
    admin_client = _app(tmp_path).test_client()
    _login(admin_client)
    started = admin_client.post(
        "/api/scenarios/7/narrative/generate",
        json={"options": {"source": "document"}},
        headers=CSRF,
    )
    job_id = started.get_json()["data"]["job_id"]
    _wait_generate_job(admin_client, job_id)

    other_client = _app(tmp_path, role="USER").test_client()
    _login(other_client, user_id=2, role="USER")
    response = other_client.get(f"/api/scenarios/7/narrative/jobs/{job_id}")

    assert response.status_code == 403



def test_narrative_start_rejects_readonly_import(tmp_path):
    _write_scenario(tmp_path, import_mode="direct", bundle=_bundle())
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.put(
        "/api/scenarios/7/narrative/start",
        json={"base_revision": 2, "start": {"location_id": "hall"}},
        headers=CSRF,
    )

    assert response.status_code == 403
