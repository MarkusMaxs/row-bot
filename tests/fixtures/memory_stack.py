from __future__ import annotations

from pathlib import Path
from typing import Any


def fresh_memory_stack(tmp_path: Path, monkeypatch, reload_for_data_dir) -> dict[str, Any]:
    data_dir = tmp_path / "row-bot-data"

    import row_bot.threads as threads

    # Extraction keeps its per-conversation watermarks with the thread metadata.
    monkeypatch.setattr(threads, "DB_PATH", str(data_dir / "threads.db"))

    (kg,) = reload_for_data_dir(data_dir, "row_bot.knowledge_graph")
    kg._skip_reindex = True
    monkeypatch.setattr(kg, "semantic_search", lambda *_args, **_kwargs: [])

    memory, memory_evolution, memory_tool, memory_extraction, wiki_vault = reload_for_data_dir(
        data_dir,
        "row_bot.memory",
        "row_bot.memory_evolution",
        "row_bot.tools.memory_tool",
        "row_bot.memory_extraction",
        "row_bot.wiki_vault",
    )

    wiki_vault._DATA_DIR = data_dir
    wiki_vault._CONFIG_PATH = wiki_vault._DATA_DIR / "wiki_config.json"

    return {
        "kg": kg,
        "memory": memory,
        "memory_evolution": memory_evolution,
        "memory_extraction": memory_extraction,
        "memory_tool": memory_tool,
        "wiki_vault": wiki_vault,
    }
