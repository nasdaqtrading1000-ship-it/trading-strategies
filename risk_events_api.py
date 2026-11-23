"""Distribucion autenticada de eventos de Acumulacion; no envia ordenes."""
import json
from datetime import datetime, timezone, timedelta
from flask import g, request, jsonify
from sqlalchemy import text, inspect


def register_risk_events_api(app, require_access):
    @app.post('/api/replicator/reductions')
    def replicator_reductions():
        require_access()
        selected = (request.get_json(silent=True) or {}).get('selected_txt_names') or []
        if 'Acumulacion.txt' not in selected:
            return jsonify(ok=True, events=[])
        allowed = g.db.execute(text("SELECT COUNT(*) FROM strategies WHERE signals_txt_name='Acumulacion.txt' AND COALESCE(replicator_available,0)=1")).scalar()
        if not allowed or not inspect(g.db).has_table('strategy_risk_events'):
            return jsonify(ok=True, events=[])
        cutoff = (datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
        rows = g.db.execute(text("SELECT payload FROM strategy_risk_events WHERE txt_name='Acumulacion.txt' AND occurred_at >= :cutoff ORDER BY occurred_at,event_id"), dict(cutoff=cutoff)).fetchall()
        return jsonify(ok=True, events=[json.loads(row[0]) for row in rows])
