"""Apply now needs a server-issued confirmation. This helper does what the UI does: preview the exact moves, then confirm them."""


def apply_moves(client, plan_id, action_ids):
    """Preview then execute. A refused preview is returned as-is (same 409 messages as a refused execute)."""
    preview = client.post(f"/api/plans/{plan_id}/preview", json={"action_ids": list(action_ids)})
    if preview.status_code != 200:
        return preview
    return client.post("/api/execute", json={"plan_id": plan_id, "action_ids": list(action_ids),
                                             "confirmation_token": preview.json()["confirmation_token"]})
