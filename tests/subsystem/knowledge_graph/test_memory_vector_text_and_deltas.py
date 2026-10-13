"""Memory search vectors: what is embedded, the text version 2 refresh, and delta generations (F54)."""
from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3

import pytest

from tests.subsystem.knowledge_graph.test_knowledge_projection_recovery import add, projection_stack as _projection_stack

projection_stack = _projection_stack
pytestmark = [pytest.mark.subsystem, pytest.mark.platform]

_PROPERTIES = {"status": "active", "source_context": {"thread_id": "synthetic-thread", "thread_name": "Synthetic"}}


def _text_version_2(entity: dict) -> str:
    """The embedded text before F54, as earlier Row-Bot versions built it."""
    parts = [entity.get("entity_type", ""), entity.get("subject", ""), entity.get("description", "")]
    for key in ("aliases", "tags"):
        if entity.get(key):
            parts.append(entity[key])
    props = json.loads(entity.get("properties") or "{}")
    if props:
        parts.append(json.dumps(props, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return " | ".join(part for part in parts if part)


def _publish_as_text_version_2(kg, monkeypatch) -> str:
    """Publish the index the way a pre-F54 Row-Bot left it: manifest version 1, text version 2."""
    with monkeypatch.context() as patch:
        patch.setattr(kg, "_entity_text", _text_version_2)
        kg.rebuild_index()
    generation = kg._projection_state()["generation"]
    path = kg._VECTOR_DIR / "generations" / generation / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(version=1, text_version=2)
    manifest.pop("base")
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(encoded)
    conn = kg._get_conn()
    try:
        conn.execute("UPDATE knowledge_vector_generations SET manifest_hash=? WHERE generation=?",
                     (hashlib.sha256(encoded).hexdigest(), generation))
        conn.commit()
    finally:
        conn.close()
    return generation


def _save_with_properties(kg, subject: str, description: str = "Synthetic saved meaning."):
    return kg.save_entity("fact", subject, description, properties=_PROPERTIES)


def _embedded(embedding) -> list[str]:
    return [text for batch in embedding.batches for text in batch]


def _found(kg, query: str = "synthetic", top_k: int = 50) -> list[str]:
    return [entity["id"] for entity in kg.semantic_search(query, top_k=top_k, threshold=-1)]


# ── What is embedded ────────────────────────────────────────────────────────


def test_only_the_meaning_is_embedded_and_property_edits_keep_the_vector(projection_stack, monkeypatch):
    from row_bot import memory_evolution

    kg, embedding, _fingerprint, _config = projection_stack
    monkeypatch.setattr(kg, "_skip_reindex", False)
    entity = kg.save_entity("fact", "Tea", "Oolong is brewed at 90C.", aliases="Wulong", tags="drinks",
                            properties=_PROPERTIES)
    assert _embedded(embedding) == ["fact | Tea | Oolong is brewed at 90C. | Wulong | drinks"]
    head = kg._projection_state()["generation"]

    # Review flags, status, provenance and edit stamps are bookkeeping, not meaning.
    memory_evolution.mark_needs_review(entity["id"], "Synthetic contradiction")
    memory_evolution.mark_user_modified(entity["id"], source_context={"surface": "synthetic", "action": "save"})
    kg.update_entity(entity["id"], "Oolong is brewed at 90C.", properties={"evidence": ["synthetic"]})

    assert len(_embedded(embedding)) == 1
    assert kg._projection_state()["generation"] == head
    assert kg.memory_vector_status()["ready"]


def test_a_content_edit_re_embeds_the_new_meaning(projection_stack, monkeypatch):
    kg, embedding, _fingerprint, _config = projection_stack
    monkeypatch.setattr(kg, "_skip_reindex", False)
    entity = _save_with_properties(kg, "Tea", "Oolong is brewed at 90C.")
    kg.update_entity(entity["id"], "Oolong is brewed at 85C.")
    assert _embedded(embedding) == ["fact | Tea | Oolong is brewed at 90C.", "fact | Tea | Oolong is brewed at 85C."]
    assert kg.memory_vector_status()["ready"]
    assert _found(kg) == [entity["id"]]


# ── Text version 2 vectors: still served, refreshed in bounded idle batches ──


def test_text_version_2_vectors_keep_serving_search_and_refresh_a_bounded_batch_at_a_time(projection_stack, monkeypatch):
    kg, embedding, _fingerprint, _config = projection_stack
    saved = [_save_with_properties(kg, f"Fact {number}") for number in range(5)]
    _publish_as_text_version_2(kg, monkeypatch)
    assert all("synthetic-thread" in text for text in _embedded(embedding))
    embedding.batches.clear()

    status = kg.memory_vector_status()
    assert status["ready"] and status["refresh_pending"] == 5
    assert sorted(_found(kg)) == sorted(entity["id"] for entity in saved)

    # A save meanwhile is appended over the version 2 generation; nothing else is re-embedded.
    monkeypatch.setattr(kg, "_skip_reindex", False)
    newer = _save_with_properties(kg, "Newer fact")
    assert _embedded(embedding) == ["fact | Newer fact | Synthetic saved meaning."]
    every = sorted([newer["id"], *(entity["id"] for entity in saved)])
    assert sorted(_found(kg)) == every
    embedding.batches.clear()

    outcomes = []
    for _ in range(3):
        outcomes.append(kg.refresh_semantic_vectors(max_entities=2))
        # Mid-way, search still finds every memory, on old and new vectors alike.
        assert kg.memory_vector_status()["ready"]
        assert sorted(_found(kg)) == every
    assert outcomes == [{"refreshed": 2, "remaining": 3}, {"refreshed": 2, "remaining": 1},
                        {"refreshed": 1, "remaining": 0}]
    assert sorted(_embedded(embedding)) == sorted(f"fact | Fact {n} | Synthetic saved meaning." for n in range(5))
    assert kg.memory_vector_status()["refresh_pending"] == 0

    monkeypatch.setattr(kg, "_get_embedding_model", lambda **_kw: pytest.fail("nothing is left to refresh"))
    assert kg.refresh_semantic_vectors(max_entities=2) == {"refreshed": 0, "remaining": 0}


def test_a_property_edit_moves_a_text_version_2_vector_once(projection_stack, monkeypatch):
    from row_bot import memory_evolution

    kg, embedding, _fingerprint, _config = projection_stack
    entity = _save_with_properties(kg, "Tea")
    _publish_as_text_version_2(kg, monkeypatch)
    embedding.batches.clear()
    monkeypatch.setattr(kg, "_skip_reindex", False)

    memory_evolution.mark_needs_review(entity["id"], "Synthetic contradiction")
    assert _embedded(embedding) == ["fact | Tea | Synthetic saved meaning."]
    assert kg.memory_vector_status()["refresh_pending"] == 0
    memory_evolution.mark_needs_review(entity["id"], "Another synthetic contradiction")
    assert len(_embedded(embedding)) == 1
    assert kg.memory_vector_status()["ready"]


def test_refresh_leaves_other_work_to_repair_and_needs_no_model(projection_stack, monkeypatch):
    kg, _embedding, _fingerprint, _config = projection_stack
    monkeypatch.setattr(kg, "_get_embedding_model", lambda **_kw: pytest.fail("refresh must not load a model here"))
    assert kg.refresh_semantic_vectors() == {"refreshed": 0, "remaining": None}
    add(kg)
    assert kg.refresh_semantic_vectors() == {"refreshed": 0, "remaining": None}
    with pytest.raises(ValueError):
        kg.refresh_semantic_vectors(max_entities=1001)


def test_idle_extraction_refreshes_a_bounded_batch_and_stops_when_the_person_returns(projection_stack, monkeypatch):
    import threading

    from row_bot import memory_extraction

    kg, embedding, _fingerprint, _config = projection_stack
    monkeypatch.setattr(memory_extraction, "_timer_stop", threading.Event())
    for number in range(3):
        _save_with_properties(kg, f"Fact {number}")
    _publish_as_text_version_2(kg, monkeypatch)
    embedding.batches.clear()
    idle = {"value": True}
    monkeypatch.setattr(memory_extraction, "is_app_idle", lambda: idle["value"])
    monkeypatch.setattr(memory_extraction, "_VECTOR_REFRESH_PER_RUN", 2)

    assert memory_extraction.refresh_memory_vectors_if_idle() == 2
    assert kg.memory_vector_status()["refresh_pending"] == 1

    idle["value"] = False
    assert memory_extraction.refresh_memory_vectors_if_idle() == 0
    assert len(_embedded(embedding)) == 2

    # The person comes back while the batch is being embedded: it is dropped, nothing is selected.
    idle["value"] = True
    head = kg._projection_state()["generation"]
    original = embedding.embed_documents

    def person_returns(texts):
        idle["value"] = False
        return original(texts)

    monkeypatch.setattr(embedding, "embed_documents", person_returns)
    assert memory_extraction.refresh_memory_vectors_if_idle() == 0
    assert kg._projection_state()["generation"] == head
    assert kg.memory_vector_status()["refresh_pending"] == 1


def test_idle_extraction_runs_the_refresh_after_reading_conversations(projection_stack, monkeypatch):
    from row_bot import memory_extraction

    calls = []
    monkeypatch.setattr(memory_extraction, "is_app_idle", lambda: True)
    monkeypatch.setattr(memory_extraction, "run_extraction", lambda **_kw: calls.append("extract") or 3)
    monkeypatch.setattr(memory_extraction, "refresh_memory_vectors_if_idle", lambda: calls.append("refresh") or 0)
    assert memory_extraction.run_extraction_if_idle() == 3
    assert calls == ["extract", "refresh"]


# ── Delta generations: a save writes its own vectors, not every segment ─────


def _generation_files(kg, generation: str) -> dict[str, tuple[bytes, int]]:
    directory = kg._VECTOR_DIR / "generations" / generation
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}


def test_single_saves_append_a_delta_and_never_rewrite_the_base(projection_stack, monkeypatch):
    import faiss

    kg, _embedding, _fingerprint, _config = projection_stack
    for number in range(20):
        add(kg, f"Base fact {number}")
    kg.rebuild_index()
    base = kg._projection_state()["generation"]
    before = _generation_files(kg, base)
    written = []
    writer = faiss.write_index

    def counting_writer(index, path):
        written.append(index.ntotal)
        writer(index, path)

    monkeypatch.setattr(faiss, "write_index", counting_writer)
    monkeypatch.setattr(kg, "_skip_reindex", False)
    first, second = add(kg, "First new fact"), add(kg, "Second new fact")

    assert written == [1, 2]  # only the rows added since the base
    metadata, _directory = kg._load_vector_generation(kg._projection_state())
    assert metadata["base"]["generation"] == base
    assert _generation_files(kg, base) == before
    assert sum(identifier is not None for identifier in metadata["ids"]) == 22
    assert kg.memory_vector_status()["ready"]
    found = _found(kg)
    assert len(found) == len(set(found)) == 22 and {first["id"], second["id"]} <= set(found)


def test_replaced_and_removed_base_rows_are_skipped_by_search(projection_stack, monkeypatch):
    kg, embedding, _fingerprint, _config = projection_stack
    # The fake embeds a text by its length, so the closest rows to a query are known.
    rows = {length: kg.save_entity("fact", "s" * length) for length in (1, 2, 3, 4, 5, 6)}
    kg.rebuild_index()
    monkeypatch.setattr(kg, "_skip_reindex", False)
    embedding.batches.clear()
    kg.update_entity(rows[1]["id"], "A much longer replacement meaning for the changed memory.")
    kg.delete_entity(rows[2]["id"])

    metadata, _directory = kg._load_vector_generation(kg._projection_state())
    assert [metadata["ids"][position] for position in metadata["dead"]] == [None, None]
    assert rows[2]["id"] not in metadata["ids"] and metadata["ids"].count(rows[1]["id"]) == 1
    assert len(_embedded(embedding)) == 1
    assert kg.memory_vector_status()["ready"]

    # The two old vectors nearest the query are gone; the next two live rows fill top_k.
    query = "q" * len("fact | s")
    assert [entity["id"] for entity in kg.semantic_search(query, top_k=2, threshold=-1)] == [
        rows[3]["id"], rows[4]["id"]]
    assert sorted(_found(kg, query)) == sorted(rows[length]["id"] for length in (1, 3, 4, 5, 6))


def test_a_delta_past_its_bound_is_published_complete_and_old_generations_retire(projection_stack, monkeypatch):
    kg, _embedding, _fingerprint, _config = projection_stack
    monkeypatch.setattr(kg, "_DELTA_MIN_ROWS", 2)
    monkeypatch.setattr(kg, "_DELTA_MAX_ROWS", 2)
    for number in range(3):
        add(kg, f"Base fact {number}")
    kg.rebuild_index()
    monkeypatch.setattr(kg, "_skip_reindex", False)
    first = kg._projection_state()["generation"]
    selected, bases = [], []
    for number in range(4):
        add(kg, f"New fact {number}")
        selected.append(kg._projection_state()["generation"])
        metadata, _directory = kg._load_vector_generation(kg._projection_state())
        bases.append(metadata["base"] and metadata["base"]["generation"])
    # Two deltas fit the bound; the third change is published complete; the next save reads that.
    assert bases == [first, first, None, selected[2]]

    # Kept: the selected delta and the complete generation it reads (also the previous one).
    directories = {path.name for path in (kg._VECTOR_DIR / "generations").iterdir()}
    assert directories == {selected[3], selected[2]}
    assert kg.memory_vector_status()["retirement_pending"] == 0
    assert kg.memory_vector_status()["ready"]
    assert len(_found(kg)) == 7


@pytest.mark.slow
def test_a_base_is_kept_while_a_kept_generation_reads_it(projection_stack, monkeypatch):
    kg, _embedding, _fingerprint, _config = projection_stack
    first = add(kg, "Base fact")
    kg.rebuild_index()
    base = kg._projection_state()["generation"]
    monkeypatch.setattr(kg, "_skip_reindex", False)
    add(kg, "Delta fact")
    add(kg, "Second delta fact")
    generations = kg._VECTOR_DIR / "generations"
    assert len(list(generations.iterdir())) == 3
    assert kg.memory_vector_status()["retirement_pending"] == 0

    # A complete publish: the previous generation (a delta) still reads the base.
    monkeypatch.setattr(kg, "_skip_reindex", True)
    kg.update_entity(first["id"], "Changed meaning published completely.")
    kg.rebuild_index()
    assert (generations / base).exists()
    # One more: nothing kept reads the base any longer, and it retires with its deltas.
    kg.update_entity(first["id"], "Changed again and published completely.")
    kg.rebuild_index()
    assert not (generations / base).exists()
    assert kg.memory_vector_status()["retirement_pending"] == 0
    assert len(list(generations.iterdir())) == 2
    assert kg.memory_vector_status()["ready"]


def test_a_failed_delta_publication_keeps_the_selected_generation_and_its_work(projection_stack, monkeypatch):
    import faiss

    kg, _embedding, _fingerprint, _config = projection_stack
    add(kg, "Base fact")
    kg.rebuild_index()
    monkeypatch.setattr(kg, "_skip_reindex", False)
    add(kg, "Delta fact")
    selected = kg._projection_state()["generation"]
    base = kg._load_vector_generation(kg._projection_state())[0]["base"]["generation"]
    before = {generation: _generation_files(kg, generation) for generation in (selected, base)}
    writer = faiss.write_index

    def fail_after_write(index, path):
        writer(index, path)
        raise OSError("synthetic publication fault")

    with monkeypatch.context() as patch:
        patch.setattr(faiss, "write_index", fail_after_write)
        failed = add(kg, "Saved while publication fails")

    assert kg.get_entity(failed["id"])
    assert kg._projection_state()["generation"] == selected
    assert {generation: _generation_files(kg, generation) for generation in (selected, base)} == before
    assert kg.memory_vector_status()["state"] == "pending"
    assert failed["id"] not in _found_without_repair(kg)
    assert kg.repair_projections()["complete"]
    assert kg.memory_vector_status()["ready"]
    assert failed["id"] in _found(kg)


def _found_without_repair(kg) -> list[str]:
    # Inside a projection batch, a pending index serves its unchanged rows and never repairs.
    with kg.projection_batch(drain_on_exit=False):
        return [entity["id"] for entity in kg.semantic_search("synthetic", top_k=50, threshold=-1)]


def test_restart_reads_the_delta_and_a_damaged_base_is_rebuilt(projection_stack, monkeypatch):
    kg, embedding, _fingerprint, _config = projection_stack
    for number in range(3):
        add(kg, f"Base fact {number}")
    kg.rebuild_index()
    monkeypatch.setattr(kg, "_skip_reindex", False)
    added = add(kg, "Delta fact")
    base = kg._load_vector_generation(kg._projection_state())[0]["base"]["generation"]

    reloaded = importlib.reload(kg)
    monkeypatch.setattr(reloaded, "_get_embedding_model", lambda **_kw: embedding)
    assert reloaded.memory_vector_status()["ready"]
    assert added["id"] in _found(reloaded)

    (reloaded._VECTOR_DIR / "generations" / base / "segment-000000.faiss").write_bytes(b"Damaged synthetic bytes")
    assert reloaded.memory_vector_status()["state"] == "failed"
    embedding.batches.clear()
    reloaded.rebuild_index()
    # The base rows are embedded again; the delta's own vector is reused.
    assert sorted(_embedded(embedding)) == sorted(
        f"fact | Base fact {number} | Synthetic deterministic knowledge body." for number in range(3))
    metadata, _directory = reloaded._load_vector_generation(reloaded._projection_state())
    assert metadata["base"] is None
    assert reloaded.memory_vector_status()["ready"]
    assert len(_found(reloaded)) == 4


def test_a_delta_whose_base_manifest_changed_is_not_trusted(projection_stack, monkeypatch):
    kg, _embedding, _fingerprint, _config = projection_stack
    add(kg, "Base fact")
    kg.rebuild_index()
    base = kg._projection_state()["generation"]
    monkeypatch.setattr(kg, "_skip_reindex", False)
    add(kg, "Delta fact")
    path = kg._VECTOR_DIR / "generations" / base / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    assert kg.memory_vector_status()["state"] == "failed"


def test_an_existing_generation_registry_gains_the_base_column(tmp_path, reload_for_data_dir):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "memory.db") as conn:
        conn.execute("""CREATE TABLE knowledge_vector_generations (
            generation TEXT PRIMARY KEY, previous_generation TEXT, manifest_hash TEXT,
            retiring INTEGER NOT NULL DEFAULT 0, validated_segments INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("INSERT INTO knowledge_vector_generations(generation,manifest_hash) VALUES(?,?)",
                     ("a" * 32, "b" * 64))
    (kg,) = reload_for_data_dir(data, "row_bot.knowledge_graph")
    conn = kg._get_conn()
    try:
        rows = [dict(row) for row in conn.execute("SELECT * FROM knowledge_vector_generations")]
    finally:
        conn.close()
    assert rows == [{"generation": "a" * 32, "previous_generation": None, "manifest_hash": "b" * 64,
                     "retiring": 0, "validated_segments": 0, "attempts": 0, "base_generation": None}]
