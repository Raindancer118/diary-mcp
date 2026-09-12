"""
Core knowledge-graph tools kept on the main diary-mcp server: creating and
reading links. The introspection/maintenance tools (path, explain, stats,
infer, report, query) moved to graph_admin.py / diary-admin-mcp — see that
module's docstring for why.

Split out of the former diary_server.py monolith (v0.10.0).
"""
import diary_db
from diary_bootstrap import mcp

_SYMMETRIC_REL_TYPES = {"related", "supports", "contradicts"}


@mcp.tool()
def memory_link(from_path: str, to_path: str, rel_type: str = "related", note: str = "") -> str:
    """Erstellt eine Verknüpfung zwischen zwei Memory-Nodes (Knowledge Graph).

    rel_type: related | supports | contradicts | requires | derived_from
    """
    valid_types = {"related", "supports", "contradicts", "requires", "derived_from"}
    if rel_type not in valid_types:
        return f"Ungültiger rel_type '{rel_type}'. Erlaubt: {', '.join(sorted(valid_types))}"
    with diary_db.get_db() as conn:
        from_node = conn.execute(
            "SELECT id FROM memory_nodes WHERE path = %s AND deleted_at IS NULL", (from_path,)
        ).fetchone()
        to_node = conn.execute(
            "SELECT id FROM memory_nodes WHERE path = %s AND deleted_at IS NULL", (to_path,)
        ).fetchone()
        if not from_node:
            return f"Quell-Node '{from_path}' nicht gefunden."
        if not to_node:
            return f"Ziel-Node '{to_path}' nicht gefunden."
        note_val = note.strip() or None

        # Symmetric rel_types (related/supports/contradicts) have no inherent direction,
        # so match either ordering before inserting — otherwise explicitly confirming a
        # link that memory_infer_links already created in the opposite direction would
        # silently create a duplicate edge instead of promoting the existing one from
        # origin='inferred' to 'explicit'. Directional types (requires/derived_from) keep
        # exact-direction matching: A-requires->B and B-requires->A are different facts.
        if rel_type in _SYMMETRIC_REL_TYPES:
            existing = conn.execute(
                "SELECT id FROM memory_links WHERE rel_type = %s AND "
                "((from_id = %s AND to_id = %s) OR (from_id = %s AND to_id = %s))",
                (rel_type, from_node["id"], to_node["id"], to_node["id"], from_node["id"]),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT id FROM memory_links WHERE rel_type = %s AND from_id = %s AND to_id = %s",
                (rel_type, from_node["id"], to_node["id"]),
            ).fetchone()

        if existing:
            conn.execute(
                "UPDATE memory_links SET note = %s, link_origin = 'explicit', updated_at = now() WHERE id = %s",
                (note_val, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO memory_links (from_id, to_id, rel_type, note, link_origin)
                   VALUES (%s, %s, %s, %s, 'explicit')""",
                (from_node["id"], to_node["id"], rel_type, note_val),
            )
    return f"Link '{from_path}' --[{rel_type}]--> '{to_path}' gespeichert."


@mcp.tool()
def memory_get_links(path: str) -> str:
    """Gibt alle eingehenden und ausgehenden Verknüpfungen eines Memory-Nodes zurück."""
    with diary_db.get_db() as conn:
        node = conn.execute(
            "SELECT id, title FROM memory_nodes WHERE path = %s AND deleted_at IS NULL", (path,)
        ).fetchone()
        if not node:
            return f"Node '{path}' nicht gefunden."
        links_out = conn.execute(
            "SELECT ml.rel_type, ml.note, mn.path AS target_path, mn.title AS target_title "
            "FROM memory_links ml JOIN memory_nodes mn ON ml.to_id = mn.id WHERE ml.from_id = %s",
            (node["id"],),
        ).fetchall()
        links_in = conn.execute(
            "SELECT ml.rel_type, mn.path AS source_path, mn.title AS source_title "
            "FROM memory_links ml JOIN memory_nodes mn ON ml.from_id = mn.id WHERE ml.to_id = %s",
            (node["id"],),
        ).fetchall()
    lines = [f"Links für '{path}' ({node['title']}):"]
    if links_out:
        lines.append("\nAusgehend:")
        for l in links_out:
            note = f" — {l['note']}" if l.get("note") else ""
            lines.append(f"  [{l['rel_type']}] → {l['target_path']} ({l['target_title']}){note}")
    if links_in:
        lines.append("\nEingehend:")
        for l in links_in:
            lines.append(f"  [{l['rel_type']}] ← {l['source_path']} ({l['source_title']})")
    if not links_out and not links_in:
        lines.append("  (Keine Verknüpfungen)")
    return "\n".join(lines)
